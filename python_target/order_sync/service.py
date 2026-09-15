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
- LOOP AT idoc_contrl WHERE mestyp = 'ORDERS' AND status = '64'
  → batch processing of message list with the same filter
"""

import logging
import uuid
from datetime import date
from decimal import Decimal
from typing import Callable, Optional

from .models import (
    DEFAULT_DOC_TYPE,
    IDOC_MESSAGE_TYPE_ORDERS,
    IDOC_STATUS_ERROR,
    IDOC_STATUS_READY,
    IDOC_STATUS_SUCCESS,
    BapiOrderPayload,
    BapiReturn,
    BapiSchedule,
    InboundOrderMessage,
    OrderCreationResult,
    OrderHeader,
    OrderItem,
    OrderPartner,
    OrderStatus,
    OrderSyncBatchResult,
    OrderSyncResult,
    PartnerRole,
)

logger = logging.getLogger(__name__)

OrderCreator = Callable[[BapiOrderPayload], OrderCreationResult]

# ABAP: LOOP AT lt_return ... WHERE type CA 'EA'.
BAPI_ERROR_TYPES = ("E", "A")

MSG_MISSING_SOLD_TO = "Missing sold-to party in IDoc"
MSG_NO_ITEMS = "No order items found in IDoc"


class OrderValidationError(Exception):
    """Raised when order data fails validation.

    Replaces ABAP: PERFORM set_idoc_status USING '51' 'E' <message>.
    """

    def __init__(self, message_id: str, errors: list[str]) -> None:
        self.message_id = message_id
        self.errors = errors
        super().__init__(f"Validation failed for {message_id}: {'; '.join(errors)}")


def validate_order(order: OrderHeader) -> list[str]:
    """Validate parsed order data before the BAPI mapping.

    Replicates the ABAP checks exactly — and only those:
      IF ls_header-sold_to IS INITIAL. → '51' 'E' 'Missing sold-to party in IDoc'. CONTINUE.
      IF lt_items IS INITIAL.          → '51' 'E' 'No order items found in IDoc'. CONTINUE.

    The first failing check ends processing of the IDoc, so at most one
    error is returned.
    """
    if not order.sold_to_party:
        return [MSG_MISSING_SOLD_TO]

    if not order.items:
        return [MSG_NO_ITEMS]

    return []


def _parse_date(value) -> Optional[date]:
    if isinstance(value, str):
        return date.fromisoformat(value) if value else None
    return value


def parse_header_segment(seg: dict, header: OrderHeader) -> None:
    """ABAP FORM parse_header_segment: doc_type = bsart, currency = curcy."""
    header.document_type = seg.get("BSART", "") or ""
    header.currency = seg.get("CURCY", "") or ""


def parse_date_segment(seg: dict, header: OrderHeader) -> None:
    """ABAP FORM parse_date_segment: CASE iddat 012/022/026."""
    qualifier = seg.get("IDDAT", "")
    date_val = _parse_date(seg.get("DATUM"))
    if qualifier == "012":
        header.requested_delivery_date = date_val
    elif qualifier == "022":
        header.customer_po_date = date_val
    elif qualifier == "026":
        header.pricing_date = date_val


def parse_partner_segment(seg: dict, header: OrderHeader) -> Optional[OrderPartner]:
    """ABAP FORM parse_partner_segment: CASE parvw AG/WE/RE/RG.

    Returns the partner to append, or None when PARVW is not handled
    (ABAP: CLEAR cs_partner → IS INITIAL → not appended).
    """
    role_code = seg.get("PARVW", "")
    partner_num = seg.get("PARTN", "") or ""

    try:
        role = PartnerRole(role_code)
    except ValueError:
        return None

    if role == PartnerRole.SOLD_TO:
        header.sold_to_party = partner_num
    elif role == PartnerRole.SHIP_TO:
        header.ship_to_party = partner_num

    return OrderPartner(role=role, number=partner_num)


def parse_item_segment(seg: dict) -> Optional[OrderItem]:
    """ABAP FORM parse_item_segment: posex, menge, menee, vprei, pstyv.

    Returns None when every parsed field is initial
    (ABAP: IF ls_item IS NOT INITIAL. APPEND ...).
    """
    item = OrderItem(
        item_number=seg.get("POSEX", "") or "",
        quantity=Decimal(str(seg.get("MENGE", 0) or 0)),
        unit_of_measure=seg.get("MENEE", "") or "",
        net_price=Decimal(str(seg.get("VPREI", 0) or 0)),
        item_category=seg.get("PSTYV") or None,
    )
    is_initial = (
        not item.item_number
        and item.quantity == 0
        and not item.unit_of_measure
        and item.net_price == 0
        and not item.item_category
    )
    return None if is_initial else item


def parse_item_material_segment(seg: dict, item: OrderItem) -> None:
    """ABAP FORM parse_item_material_segment: CASE qualf 002/003."""
    qualifier = seg.get("QUALF", "")
    identifier = seg.get("IDTNR", "") or ""
    if qualifier == "002":
        item.material_number = identifier
    elif qualifier == "003":
        item.customer_material = identifier


def parse_idoc_to_order(raw_segments: list[dict]) -> OrderHeader:
    """Parse raw IDoc-like segment data into an OrderHeader.

    Mirrors the ABAP segment loop (CASE lv_segment / WHEN 'E1EDK01' ...).

    Segment mapping:
      E1EDK01 → document_type, currency
      E1EDK03 → dates (qualifier-based: 012=delivery, 022=PO date, 026=pricing)
      E1EDKA1 → partners (AG=sold-to, WE=ship-to, RE=bill-to, RG=payer)
      E1EDP01 → item number, quantity, UOM, price, category
      E1EDP19 → material identifiers on the LAST appended item
                (002=SAP material, 003=customer material)
    """
    header = OrderHeader()

    for seg in raw_segments:
        seg_type = seg.get("segment_type", "")

        if seg_type == "E1EDK01":
            parse_header_segment(seg, header)

        elif seg_type == "E1EDK03":
            parse_date_segment(seg, header)

        elif seg_type == "E1EDKA1":
            partner = parse_partner_segment(seg, header)
            if partner is not None:
                header.partners.append(partner)

        elif seg_type == "E1EDP01":
            item = parse_item_segment(seg)
            if item is not None:
                header.items.append(item)

        elif seg_type == "E1EDP19":
            # ABAP: IF lt_items IS NOT INITIAL. ... CHANGING lt_items[ lines( lt_items ) ].
            if header.items:
                parse_item_material_segment(seg, header.items[-1])

    return header


def map_to_bapi_payload(order: OrderHeader) -> BapiOrderPayload:
    """Map the parsed order to the BAPI parameter set.

    ABAP "Map to BAPI Structures" block:
      doc_type defaults to 'ZOR' when initial; header X-flags for
      doc_type/sales_org/distr_chan/division/purch_no + updateflag 'I';
      one schedule line '0001' per item with req_date = header req_dlv_date
      and req_qty = item quantity.
    """
    header = order.model_copy(deep=True)
    if not header.document_type:
        header.document_type = DEFAULT_DOC_TYPE

    header_flags = {
        "doc_type": "X",
        "sales_org": "X",
        "distr_chan": "X",
        "division": "X",
        "purch_no": "X",
        "updateflag": "I",
    }

    schedules = [
        BapiSchedule(
            item_number=item.item_number,
            schedule_line="0001",
            requested_date=header.requested_delivery_date,
            requested_quantity=item.quantity,
        )
        for item in header.items
    ]

    return BapiOrderPayload(
        header=header,
        header_flags=header_flags,
        items=header.items,
        partners=header.partners,
        schedules=schedules,
    )


def set_idoc_status(
    message_id: str,
    idoc_status: str,
    message_type: str,
    message: str,
    order_number: Optional[str] = None,
    error_messages: Optional[list[str]] = None,
) -> OrderSyncResult:
    """ABAP FORM set_idoc_status: msgv1 = first 50 chars, msgv2 = next 50."""
    return OrderSyncResult(
        message_id=message_id,
        status=OrderStatus.CREATED if idoc_status == IDOC_STATUS_SUCCESS else OrderStatus.FAILED,
        idoc_status=idoc_status,
        message_type=message_type,
        message_v1=message[:50],
        message_v2=message[50:100] if len(message) > 50 else "",
        order_number=order_number,
        error_messages=error_messages or [],
    )


def is_processable(message: InboundOrderMessage) -> bool:
    """ABAP: LOOP AT idoc_contrl WHERE mestyp = 'ORDERS' AND status = '64'."""
    return (
        message.message_type == IDOC_MESSAGE_TYPE_ORDERS
        and message.status == IDOC_STATUS_READY
    )


def process_single_order(
    message: InboundOrderMessage,
    create_order: Optional[OrderCreator] = None,
) -> OrderSyncResult:
    """Process a single inbound order message.

    Replaces the inner body of ABAP: LOOP AT idoc_contrl INTO ls_idoc_ctrl.

    1. Validate (sold-to present, items present) → status '51' on failure
    2. Map to BAPI payload
    3. Call the target system (replaces BAPI_SALESORDER_CREATEFROMDAT2)
    4. Any return message of type E/A → rollback, status '51' with the
       concatenated error texts; otherwise commit, status '53'
    """
    if create_order is None:
        create_order = _create_order_in_target_system

    order = message.order

    errors = validate_order(order)
    if errors:
        logger.warning(
            "Order validation failed for message %s: %s",
            message.message_id,
            errors,
        )
        return set_idoc_status(
            message.message_id, IDOC_STATUS_ERROR, "E", errors[0], error_messages=errors
        )

    payload = map_to_bapi_payload(order)

    # Replaces: CALL FUNCTION 'BAPI_SALESORDER_CREATEFROMDAT2'
    # A transport/target failure is treated like an aborting ('A') return so the
    # IDoc gets status 51 and the rest of the batch keeps processing.
    try:
        creation = create_order(payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Order creation raised for message %s", message.message_id)
        creation = OrderCreationResult(messages=[BapiReturn(type="A", message=str(exc))])

    # ABAP: LOOP AT lt_return WHERE type CA 'EA'. lv_has_error = abap_true.
    error_returns = [m for m in creation.messages if m.type in BAPI_ERROR_TYPES]

    if error_returns:
        # Replaces: CALL FUNCTION 'BAPI_TRANSACTION_ROLLBACK'
        # ABAP: REDUCE ... NEXT msg = |{ msg }{ wa-message }; |
        err_msg = "".join(f"{m.message}; " for m in error_returns)
        logger.error(
            "Order creation failed for message %s: %s", message.message_id, err_msg
        )
        return set_idoc_status(
            message.message_id,
            IDOC_STATUS_ERROR,
            "E",
            err_msg,
            error_messages=[m.message for m in error_returns],
        )

    # Replaces: CALL FUNCTION 'BAPI_TRANSACTION_COMMIT' EXPORTING wait = 'X'
    logger.info(
        "Order %s created for message %s (sold-to: %s, %d items)",
        creation.order_number,
        message.message_id,
        order.sold_to_party,
        len(order.items),
    )
    return set_idoc_status(
        message.message_id,
        IDOC_STATUS_SUCCESS,
        "S",
        f"Sales order {creation.order_number} created",
        order_number=creation.order_number,
    )


def process_order_batch(
    messages: list[InboundOrderMessage],
    create_order: Optional[OrderCreator] = None,
) -> OrderSyncBatchResult:
    """Process a batch of inbound order messages.

    Replaces the outer LOOP AT idoc_contrl in the ABAP function module.
    Messages whose type/status do not match the ABAP WHERE clause are
    skipped and produce no status record.
    """
    results: list[OrderSyncResult] = []
    skipped = 0

    for message in messages:
        if not is_processable(message):
            skipped += 1
            continue
        results.append(process_single_order(message, create_order))

    successful = sum(1 for r in results if r.status == OrderStatus.CREATED)
    failed = sum(1 for r in results if r.status == OrderStatus.FAILED)

    return OrderSyncBatchResult(
        total_processed=len(results),
        successful=successful,
        failed=failed,
        skipped=skipped,
        results=results,
    )


def _create_order_in_target_system(payload: BapiOrderPayload) -> OrderCreationResult:
    """Simulate creating an order in the target system.

    In production, this would call the target ERP/OMS API.
    For the demo, generates a synthetic order number and no error messages.

    Replaces: CALL FUNCTION 'BAPI_SALESORDER_CREATEFROMDAT2'
              IMPORTING salesdocument = lv_vbeln TABLES return = lt_return
    """
    # Generate a 10-digit order number (similar to SAP VBELN format)
    order_number = str(uuid.uuid4().int)[:10].zfill(10)
    return OrderCreationResult(
        order_number=order_number,
        messages=[BapiReturn(type="S", message=f"Sales order {order_number} created")],
    )
