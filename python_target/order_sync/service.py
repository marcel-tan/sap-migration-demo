"""
Order Sync Service — migrated from Z_IDOC_ORDER_SYNC.

Original ABAP: IDoc processing function for inbound ORDERS05 IDocs.
               Parses IDoc segments, validates, and calls
               BAPI_SALESORDER_CREATEFROMDAT2 to create sales orders.
Target:        Event-driven Python service consuming order messages
               from a queue and persisting to the target system.

Migration notes:
- IDoc segment parsing (E1EDK01, E1EDK03, E1EDKA1, E1EDP01, E1EDP19)
  → JSON deserialization into Pydantic models
- BAPI_SALESORDER_CREATEFROMDAT2 → target system API / ORM
- IDoc status records (51=error, 53=success) → OrderSyncResult
- BAPI_TRANSACTION_COMMIT/ROLLBACK → database transaction management
- LOOP AT idoc_contrl → batch processing of message list
"""

import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from typing import Callable, Iterator, Optional

from ..feature_flags import (
    ORDER_SYNC,
    FieldDiff,
    LegacyHandlerRequired,
    OrderSyncFlagMode,
    RoutedResult,
    ShadowDiff,
    emit_flag_event,
    resolve_flag,
)
from .models import (
    InboundOrderMessage,
    OrderHeader,
    OrderItem,
    OrderPartner,
    OrderStatus,
    OrderSyncBatchResult,
    OrderSyncResult,
    PartnerRole,
)

logger = logging.getLogger(__name__)


class OrderValidationError(Exception):
    """Raised when order data fails validation.

    Replaces ABAP: PERFORM set_idoc_status USING '51' 'E' <message>.
    """

    def __init__(self, message_id: str, errors: list[str]) -> None:
        self.message_id = message_id
        self.errors = errors
        super().__init__(f"Validation failed for {message_id}: {'; '.join(errors)}")


def validate_order(order: OrderHeader) -> list[str]:
    """Validate parsed order data before processing.

    Replicates ABAP validation checks:
    - Sold-to party is required
    - At least one order item is required
    - All items must have positive quantity
    - Sales org / dist channel / division are required
    """
    errors: list[str] = []

    if not order.sold_to_party:
        errors.append("Missing sold-to party (AG partner)")

    if not order.items:
        errors.append("No order items found")

    if not order.sales_org:
        errors.append("Missing sales organization")

    if not order.distribution_channel:
        errors.append("Missing distribution channel")

    if not order.division:
        errors.append("Missing division")

    for item in order.items:
        if item.quantity <= 0:
            errors.append(
                f"Item {item.item_number}: quantity must be positive "
                f"(got {item.quantity})"
            )
        if not item.material_number and not item.customer_material:
            errors.append(
                f"Item {item.item_number}: "
                f"material_number or customer_material required"
            )

    return errors


def parse_idoc_to_order(raw_segments: list[dict]) -> OrderHeader:
    """Parse raw IDoc-like segment data into an OrderHeader.

    This function demonstrates the migration path from ABAP's segment-by-segment
    IDoc parsing (CASE lv_segment / WHEN 'E1EDK01' ...) to structured
    deserialization. In production, inbound messages would already be JSON,
    but this function shows the mapping for documentation purposes.

    Segment mapping:
      E1EDK01 → document_type, currency
      E1EDK03 → dates (qualifier-based: 012=delivery, 022=PO date, 026=pricing)
      E1EDKA1 → partners (AG=sold-to, WE=ship-to, RE=bill-to, RG=payer)
      E1EDP01 → item number, quantity, UOM, price, category
      E1EDP19 → material identifiers (002=SAP material, 003=customer material)
    """
    header = OrderHeader(
        sales_org="",
        distribution_channel="",
        division="",
        sold_to_party="",
    )
    current_item: Optional[OrderItem] = None

    for seg in raw_segments:
        seg_type = seg.get("segment_type", "")

        if seg_type == "E1EDK01":
            header.document_type = seg.get("BSART", "ZOR")
            header.currency = seg.get("CURCY", "USD")
            header.sales_org = seg.get("SALES_ORG", "")
            header.distribution_channel = seg.get("DISTR_CHAN", "")
            header.division = seg.get("DIVISION", "")

        elif seg_type == "E1EDK03":
            qualifier = seg.get("IDDAT", "")
            date_val = seg.get("DATUM")
            if isinstance(date_val, str):
                date_val = None if not date_val.strip("0") else date.fromisoformat(date_val)
            if qualifier == "012":
                header.requested_delivery_date = date_val
            elif qualifier == "022":
                header.customer_po_date = date_val
            elif qualifier == "026":
                header.pricing_date = date_val

        elif seg_type == "E1EDKA1":
            role_code = seg.get("PARVW", "")
            partner_num = seg.get("PARTN", "")

            try:
                role = PartnerRole(role_code)
            except ValueError:
                continue

            partner = OrderPartner(role=role, number=partner_num)
            header.partners.append(partner)

            if role == PartnerRole.SOLD_TO:
                header.sold_to_party = partner_num
            elif role == PartnerRole.SHIP_TO:
                header.ship_to_party = partner_num

        elif seg_type == "E1EDP01":
            current_item = OrderItem(
                item_number=seg.get("POSEX", "000010"),
                quantity=Decimal(str(seg.get("MENGE", 0))),
                unit_of_measure=seg.get("MENEE", "EA"),
                net_price=Decimal(str(seg.get("VPREI", 0))),
                item_category=seg.get("PSTYV"),
                plant=seg.get("PLANT"),
            )
            header.items.append(current_item)

        elif seg_type == "E1EDP19":
            if current_item is not None:
                qualifier = seg.get("QUALF", "")
                identifier = seg.get("IDTNR", "")
                if qualifier == "002":
                    current_item.material_number = identifier
                elif qualifier == "003":
                    current_item.customer_material = identifier

    return header


OrderCreator = Callable[[OrderHeader], str]


def process_single_order(
    message: InboundOrderMessage,
    order_creator: Optional[OrderCreator] = None,
) -> OrderSyncResult:
    """Process a single inbound order message.

    Replaces the inner body of ABAP: LOOP AT idoc_contrl INTO ls_idoc_ctrl.

    In production, this would:
    1. Validate the order
    2. Call the target system's order creation API (replaces BAPI call)
    3. Handle commit/rollback
    4. Return status

    For the demo, we simulate the order creation and return a result.
    """
    order = message.order

    # Validate — replaces ABAP validation checks before BAPI call
    errors = validate_order(order)
    if errors:
        logger.warning(
            "Order validation failed for message %s: %s",
            message.message_id,
            errors,
        )
        return OrderSyncResult(
            message_id=message.message_id,
            status=OrderStatus.FAILED,
            error_messages=errors,
        )

    # Simulate order creation — in production, this calls the target system
    # Replaces: CALL FUNCTION 'BAPI_SALESORDER_CREATEFROMDAT2'
    try:
        order_number = (order_creator or _create_order_in_target_system)(order)

        logger.info(
            "Order %s created for message %s (sold-to: %s, %d items)",
            order_number,
            message.message_id,
            order.sold_to_party,
            len(order.items),
        )

        # Replaces: CALL FUNCTION 'BAPI_TRANSACTION_COMMIT' EXPORTING wait = 'X'
        return OrderSyncResult(
            message_id=message.message_id,
            status=OrderStatus.CREATED,
            order_number=order_number,
        )

    except Exception as exc:
        # Replaces: CALL FUNCTION 'BAPI_TRANSACTION_ROLLBACK'
        logger.error(
            "Order creation failed for message %s: %s",
            message.message_id,
            exc,
        )
        return OrderSyncResult(
            message_id=message.message_id,
            status=OrderStatus.FAILED,
            error_messages=[str(exc)],
        )


def process_order_batch(
    messages: list[InboundOrderMessage],
) -> OrderSyncBatchResult:
    """Process a batch of inbound order messages.

    Replaces the outer LOOP AT idoc_contrl in the ABAP function module.
    Each message is processed independently (matches IDoc-by-IDoc processing).
    """
    results: list[OrderSyncResult] = []

    for message in messages:
        result = process_single_order(message)
        results.append(result)

    successful = sum(1 for r in results if r.status == OrderStatus.CREATED)
    failed = sum(1 for r in results if r.status == OrderStatus.FAILED)

    return OrderSyncBatchResult(
        total_processed=len(results),
        successful=successful,
        failed=failed,
        results=results,
    )


def _create_order_in_target_system(order: OrderHeader) -> str:
    """Simulate creating an order in the target system.

    In production, this would call the target ERP/OMS API.
    For the demo, generates a synthetic order number.

    Replaces: CALL FUNCTION 'BAPI_SALESORDER_CREATEFROMDAT2' ... IMPORTING salesdocument = lv_vbeln
    """
    # Generate a 10-digit order number (similar to SAP VBELN format)
    order_number = str(uuid.uuid4().int)[:10].zfill(10)
    return order_number


# ---------------------------------------------------------------------------
# Feature-flag routing (IDoc/ABAP → validate_only → Python)
# ---------------------------------------------------------------------------

# Legacy path: hands the IDoc to the ABAP inbound function Z_IDOC_ORDER_SYNC.
OrderLegacyHandler = Callable[[InboundOrderMessage], OrderSyncResult]

IDOC_STATUS = {OrderStatus.CREATED: "53", OrderStatus.FAILED: "51"}


class InMemoryIdempotencyStore:
    """Created orders keyed by IDoc DOCNUM, shared by the ABAP and Python paths.

    Production would back this with a DB table with a unique key on DOCNUM.
    Only successful creations are stored, so a status-51 IDoc can be reprocessed.
    """

    def __init__(self) -> None:
        self._results: dict[str, OrderSyncResult] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @contextmanager
    def lock(self, docnum: str) -> Iterator[None]:
        with self._guard:
            doc_lock = self._locks.setdefault(docnum, threading.Lock())
        with doc_lock:
            yield

    def get(self, docnum: str) -> Optional[OrderSyncResult]:
        return self._results.get(docnum)

    def put(self, docnum: str, result: OrderSyncResult) -> None:
        self._results[docnum] = result


_default_store = InMemoryIdempotencyStore()


def _python_prediction(message: InboundOrderMessage) -> OrderSyncResult:
    """What the Python path would do, without calling order creation."""
    errors = validate_order(message.order)
    return OrderSyncResult(
        message_id=message.message_id,
        status=OrderStatus.FAILED if errors else OrderStatus.VALIDATED,
        error_messages=errors,
    )


def diff_order_outcomes(legacy: OrderSyncResult, predicted: OrderSyncResult) -> ShadowDiff:
    """Compare the ABAP IDoc outcome with the Python validation prediction."""
    diff = ShadowDiff()
    legacy_ok = legacy.status == OrderStatus.CREATED
    python_ok = predicted.status == OrderStatus.VALIDATED
    if legacy_ok != python_ok:
        diff.differences.append(
            FieldDiff(
                legacy.message_id,
                "idoc_status",
                IDOC_STATUS.get(legacy.status),
                "53" if python_ok else "51",
            )
        )
    return diff


def route_order_message(
    message: InboundOrderMessage,
    *,
    legacy_handler: Optional[OrderLegacyHandler] = None,
    order_creator: Optional[OrderCreator] = None,
    idempotency_store: Optional[InMemoryIdempotencyStore] = None,
    flags_config: Optional[dict] = None,
    env: Optional[dict] = None,
) -> RoutedResult[OrderSyncResult]:
    """Process one inbound order according to the order_sync flag.

    Targeting: ``docnum`` (message_id), ``sndprn`` (sender partner) and ``kunnr`` (sold-to).
    VALIDATE_ONLY lets ABAP create the order and records what Python would have done.
    Every path is idempotent on DOCNUM: a replayed IDoc returns the stored result.
    """
    store = idempotency_store or _default_store
    docnum = message.message_id
    attributes = {
        "docnum": docnum,
        "sndprn": message.sender_system,
        "kunnr": message.order.sold_to_party,
    }
    decision = resolve_flag(
        ORDER_SYNC,
        {k: v for k, v in attributes.items() if v},
        config=flags_config,
        env=env,
    )

    with store.lock(docnum):
        existing = store.get(docnum)
        if existing is not None:
            routed = RoutedResult(
                existing, decision, "idempotency_store", details={"replayed": True}
            )
        elif decision.mode == OrderSyncFlagMode.PYTHON:
            result = process_single_order(message, order_creator=order_creator)
            routed = RoutedResult(result, decision, "python")
        else:
            if legacy_handler is None:
                raise LegacyHandlerRequired(ORDER_SYNC, decision.mode)
            result = legacy_handler(message)
            routed = RoutedResult(result, decision, "abap")
            if decision.mode == OrderSyncFlagMode.VALIDATE_ONLY:
                try:
                    predicted = _python_prediction(message)
                    routed.diff = diff_order_outcomes(result, predicted)
                    routed.details["would_have"] = {
                        "action": "create_order" if predicted.status == OrderStatus.VALIDATED else "reject",
                        "idoc_status": "53" if predicted.status == OrderStatus.VALIDATED else "51",
                        "sold_to": message.order.sold_to_party,
                        "item_count": len(message.order.items),
                        "errors": predicted.error_messages,
                    }
                except Exception as exc:  # shadow path must never break the legacy path
                    routed.diff = ShadowDiff(python_error=repr(exc))

        if routed.served_by != "idempotency_store" and routed.result.status == OrderStatus.CREATED:
            store.put(docnum, routed.result)

    routed.details["idoc_status"] = IDOC_STATUS.get(routed.result.status)
    emit_flag_event(routed)
    return routed


def route_inbound_idoc(
    docnum: str,
    raw_segments: list[dict],
    *,
    sender_partner: Optional[str] = None,
    **kwargs,
) -> RoutedResult[OrderSyncResult]:
    """Parse raw ORDERS05 segments, then route via ``route_order_message``."""
    message = InboundOrderMessage(
        message_id=docnum,
        sender_system=sender_partner,
        order=parse_idoc_to_order(raw_segments),
    )
    return route_order_message(message, **kwargs)
