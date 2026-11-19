"""
Tests for the Order Sync service.

Validates functional equivalence between the ABAP Z_IDOC_ORDER_SYNC
function module and the migrated Python implementation.
"""

from datetime import date
from decimal import Decimal
import logging

import pytest

from python_target.feature_flags import OrderSyncFlagMode
from python_target.order_sync.models import (
    InboundOrderMessage,
    OrderHeader,
    OrderItem,
    OrderPartner,
    OrderStatus,
    OrderSyncResult,
    PartnerRole,
)
from python_target.order_sync.service import (
    InMemoryIdempotencyStore,
    OrderValidationError,
    parse_idoc_to_order,
    process_order_batch,
    process_single_order,
    route_inbound_idoc,
    route_order_message,
    validate_order,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def valid_order() -> OrderHeader:
    """A fully valid order matching a typical ORDERS05 IDoc."""
    return OrderHeader(
        document_type="ZOR",
        sales_org="1000",
        distribution_channel="10",
        division="00",
        sold_to_party="CUST-001",
        ship_to_party="CUST-001",
        customer_po_number="PO-2024-5678",
        customer_po_date=date(2024, 6, 15),
        requested_delivery_date=date(2024, 7, 1),
        currency="USD",
        partners=[
            OrderPartner(role=PartnerRole.SOLD_TO, number="CUST-001"),
            OrderPartner(role=PartnerRole.SHIP_TO, number="CUST-001"),
        ],
        items=[
            OrderItem(
                item_number="000010",
                material_number="MAT-001",
                quantity=Decimal("100"),
                unit_of_measure="EA",
                net_price=Decimal("25.50"),
                plant="1000",
            ),
            OrderItem(
                item_number="000020",
                material_number="MAT-002",
                customer_material="CUST-MAT-002",
                quantity=Decimal("50"),
                unit_of_measure="EA",
                net_price=Decimal("42.00"),
                plant="1000",
            ),
        ],
    )


@pytest.fixture()
def valid_message(valid_order) -> InboundOrderMessage:
    return InboundOrderMessage(
        message_id="MSG-001",
        message_type="ORDERS",
        sender_system="EDI-GATEWAY",
        order=valid_order,
    )


@pytest.fixture()
def sample_idoc_segments() -> list[dict]:
    """Raw IDoc segment data — simulates the EDIDD table rows."""
    return [
        {
            "segment_type": "E1EDK01",
            "BSART": "ZOR",
            "CURCY": "USD",
            "SALES_ORG": "1000",
            "DISTR_CHAN": "10",
            "DIVISION": "00",
        },
        {
            "segment_type": "E1EDK03",
            "IDDAT": "012",
            "DATUM": "2024-07-01",
        },
        {
            "segment_type": "E1EDK03",
            "IDDAT": "022",
            "DATUM": "2024-06-15",
        },
        {
            "segment_type": "E1EDKA1",
            "PARVW": "AG",
            "PARTN": "CUST-001",
        },
        {
            "segment_type": "E1EDKA1",
            "PARVW": "WE",
            "PARTN": "CUST-002",
        },
        {
            "segment_type": "E1EDP01",
            "POSEX": "000010",
            "MENGE": 100,
            "MENEE": "EA",
            "VPREI": 25.50,
        },
        {
            "segment_type": "E1EDP19",
            "QUALF": "002",
            "IDTNR": "MAT-001",
        },
        {
            "segment_type": "E1EDP19",
            "QUALF": "003",
            "IDTNR": "CUST-MAT-A",
        },
        {
            "segment_type": "E1EDP01",
            "POSEX": "000020",
            "MENGE": 50,
            "MENEE": "EA",
            "VPREI": 42.00,
        },
        {
            "segment_type": "E1EDP19",
            "QUALF": "002",
            "IDTNR": "MAT-002",
        },
    ]


# ---------------------------------------------------------------------------
# validate_order — mirrors ABAP validation before BAPI call
# ---------------------------------------------------------------------------

class TestValidateOrder:

    def test_valid_order_passes(self, valid_order):
        errors = validate_order(valid_order)
        assert errors == []

    def test_missing_sold_to(self, valid_order):
        """ABAP: IF ls_header-sold_to IS INITIAL → status '51'."""
        valid_order.sold_to_party = ""
        errors = validate_order(valid_order)
        assert any("sold-to" in e.lower() for e in errors)

    def test_no_items(self, valid_order):
        """ABAP: IF lt_items IS INITIAL → status '51'."""
        valid_order.items = []
        errors = validate_order(valid_order)
        assert any("no order items" in e.lower() for e in errors)

    def test_zero_quantity_item(self, valid_order):
        """Items with non-positive quantity are flagged."""
        valid_order.items[0].quantity = Decimal("0")
        errors = validate_order(valid_order)
        assert any("quantity" in e.lower() for e in errors)

    def test_missing_material_and_customer_material(self, valid_order):
        valid_order.items[0].material_number = None
        valid_order.items[0].customer_material = None
        errors = validate_order(valid_order)
        assert any("material" in e.lower() for e in errors)

    def test_missing_sales_org(self, valid_order):
        valid_order.sales_org = ""
        errors = validate_order(valid_order)
        assert any("sales organization" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# parse_idoc_to_order — mirrors ABAP segment parsing CASE/WHEN logic
# ---------------------------------------------------------------------------

class TestParseIdocToOrder:

    def test_header_fields(self, sample_idoc_segments):
        """ABAP: WHEN 'E1EDK01' → PERFORM parse_header_segment."""
        order = parse_idoc_to_order(sample_idoc_segments)
        assert order.document_type == "ZOR"
        assert order.currency == "USD"
        assert order.sales_org == "1000"

    def test_date_parsing(self, sample_idoc_segments):
        """ABAP: WHEN 'E1EDK03' with IDDAT qualifiers 012, 022, 026."""
        order = parse_idoc_to_order(sample_idoc_segments)
        assert order.requested_delivery_date == date(2024, 7, 1)
        assert order.customer_po_date == date(2024, 6, 15)

    def test_partner_parsing(self, sample_idoc_segments):
        """ABAP: WHEN 'E1EDKA1' → sold-to (AG), ship-to (WE)."""
        order = parse_idoc_to_order(sample_idoc_segments)
        assert order.sold_to_party == "CUST-001"
        assert order.ship_to_party == "CUST-002"
        assert len(order.partners) == 2

    def test_item_parsing(self, sample_idoc_segments):
        """ABAP: WHEN 'E1EDP01' → item number, qty, UOM, price."""
        order = parse_idoc_to_order(sample_idoc_segments)
        assert len(order.items) == 2
        assert order.items[0].item_number == "000010"
        assert order.items[0].quantity == Decimal("100")
        assert order.items[1].item_number == "000020"

    def test_material_identification(self, sample_idoc_segments):
        """ABAP: WHEN 'E1EDP19' → qualifier 002=SAP material, 003=customer material."""
        order = parse_idoc_to_order(sample_idoc_segments)
        assert order.items[0].material_number == "MAT-001"
        assert order.items[0].customer_material == "CUST-MAT-A"
        assert order.items[1].material_number == "MAT-002"


# ---------------------------------------------------------------------------
# process_single_order — mirrors ABAP per-IDoc processing loop body
# ---------------------------------------------------------------------------

class TestProcessSingleOrder:

    def test_successful_order_creation(self, valid_message):
        """ABAP: BAPI returns no errors → BAPI_TRANSACTION_COMMIT → status '53'."""
        result = process_single_order(valid_message)

        assert result.status == OrderStatus.CREATED
        assert result.order_number is not None
        assert len(result.order_number) == 10  # SAP VBELN format
        assert result.error_messages == []

    def test_validation_failure(self, valid_message):
        """ABAP: Validation fails → set_idoc_status '51' 'E'."""
        valid_message.order.sold_to_party = ""
        result = process_single_order(valid_message)

        assert result.status == OrderStatus.FAILED
        assert len(result.error_messages) > 0


# ---------------------------------------------------------------------------
# process_order_batch — mirrors ABAP LOOP AT idoc_contrl
# ---------------------------------------------------------------------------

class TestProcessOrderBatch:

    def test_mixed_batch(self, valid_order):
        """Process batch with mix of valid and invalid orders."""
        messages = [
            InboundOrderMessage(
                message_id="MSG-001",
                order=valid_order,
            ),
            InboundOrderMessage(
                message_id="MSG-002",
                order=OrderHeader(
                    sales_org="1000",
                    distribution_channel="10",
                    division="00",
                    sold_to_party="",  # Invalid: missing sold-to
                    items=[],          # Invalid: no items
                ),
            ),
            InboundOrderMessage(
                message_id="MSG-003",
                order=valid_order,
            ),
        ]

        batch_result = process_order_batch(messages)

        assert batch_result.total_processed == 3
        assert batch_result.successful == 2
        assert batch_result.failed == 1
        assert batch_result.results[1].status == OrderStatus.FAILED

    def test_empty_batch(self):
        batch_result = process_order_batch([])
        assert batch_result.total_processed == 0
        assert batch_result.successful == 0
        assert batch_result.failed == 0


# ---------------------------------------------------------------------------
# route_order_message — feature-flag routing (idoc_abap → validate_only → python)
# ---------------------------------------------------------------------------

VALIDATE_ONLY_ENV = {"FF_ORDER_SYNC_MODE": "validate_only"}
PYTHON_ENV = {"FF_ORDER_SYNC_MODE": "python"}


class _Recorder:
    """Callable stub that counts calls — stands in for ABAP inbound / order create."""

    def __init__(self, returns):
        self.calls = 0
        self._returns = returns

    def __call__(self, arg):
        self.calls += 1
        return self._returns(arg) if callable(self._returns) else self._returns


def _abap_created(message):
    return OrderSyncResult(
        message_id=message.message_id, status=OrderStatus.CREATED, order_number="0000012345"
    )


class TestRouteOrderMessage:

    def test_route_order_message_defaults_to_idoc_abap(self, valid_message):
        legacy, creator = _Recorder(_abap_created), _Recorder("0000099999")

        routed = route_order_message(
            valid_message, legacy_handler=legacy, order_creator=creator,
            idempotency_store=InMemoryIdempotencyStore(), env={},
        )

        assert routed.decision.mode == OrderSyncFlagMode.IDOC_ABAP
        assert routed.result.order_number == "0000012345"
        assert (legacy.calls, creator.calls) == (1, 0)

    def test_route_order_message_validate_only_does_not_create_order(self, valid_message, caplog):
        legacy, creator = _Recorder(_abap_created), _Recorder("0000099999")

        with caplog.at_level(logging.INFO, logger="python_target.feature_flags"):
            routed = route_order_message(
                valid_message, legacy_handler=legacy, order_creator=creator,
                idempotency_store=InMemoryIdempotencyStore(), env=VALIDATE_ONLY_ENV,
            )

        assert creator.calls == 0
        assert routed.served_by == "abap"
        assert routed.result.order_number == "0000012345"
        assert routed.diff.clean
        event = caplog.records[-1].flag_event
        assert event["mode"] == "validate_only"
        assert event["would_have"]["action"] == "create_order"
        assert event["would_have"]["item_count"] == 2

    def test_route_order_message_validate_only_records_rejection_diff(self, valid_message):
        valid_message.order.sold_to_party = ""
        creator = _Recorder("0000099999")

        routed = route_order_message(
            valid_message, legacy_handler=_abap_created, order_creator=creator,
            idempotency_store=InMemoryIdempotencyStore(), env=VALIDATE_ONLY_ENV,
        )

        assert creator.calls == 0
        assert routed.details["would_have"]["action"] == "reject"
        assert routed.details["would_have"]["idoc_status"] == "51"
        diff = routed.diff.differences[0]
        assert (diff.field, diff.legacy, diff.python) == ("idoc_status", "53", "51")

    def test_route_order_message_python_mode_creates_order(self, valid_message):
        legacy, creator = _Recorder(_abap_created), _Recorder("0000099999")

        routed = route_order_message(
            valid_message, legacy_handler=legacy, order_creator=creator,
            idempotency_store=InMemoryIdempotencyStore(), env=PYTHON_ENV,
        )

        assert routed.served_by == "python"
        assert routed.result.order_number == "0000099999"
        assert routed.details["idoc_status"] == "53"
        assert (legacy.calls, creator.calls) == (0, 1)

    def test_route_order_message_replay_same_docnum_is_idempotent(self, valid_message):
        store, creator = InMemoryIdempotencyStore(), _Recorder("0000099999")

        first = route_order_message(
            valid_message, order_creator=creator, idempotency_store=store, env=PYTHON_ENV
        )
        replay = route_order_message(
            valid_message, order_creator=creator, idempotency_store=store, env=PYTHON_ENV
        )

        assert creator.calls == 1
        assert replay.served_by == "idempotency_store"
        assert replay.details["replayed"] is True
        assert replay.result.order_number == first.result.order_number

    def test_route_order_message_flag_flip_after_abap_create_does_not_duplicate(self, valid_message):
        store, creator = InMemoryIdempotencyStore(), _Recorder("0000099999")

        route_order_message(
            valid_message, legacy_handler=_abap_created, idempotency_store=store,
            env=VALIDATE_ONLY_ENV,
        )
        flipped = route_order_message(
            valid_message, order_creator=creator, idempotency_store=store, env=PYTHON_ENV
        )

        assert creator.calls == 0
        assert flipped.result.order_number == "0000012345"

    def test_route_order_message_failed_idoc_can_be_reprocessed(self, valid_message):
        store, creator = InMemoryIdempotencyStore(), _Recorder("0000099999")
        sold_to = valid_message.order.sold_to_party
        valid_message.order.sold_to_party = ""

        failed = route_order_message(
            valid_message, order_creator=creator, idempotency_store=store, env=PYTHON_ENV
        )
        valid_message.order.sold_to_party = sold_to
        retried = route_order_message(
            valid_message, order_creator=creator, idempotency_store=store, env=PYTHON_ENV
        )

        assert failed.details["idoc_status"] == "51"
        assert retried.result.status == OrderStatus.CREATED
        assert creator.calls == 1

    def test_route_order_message_targets_docnum_cohort(self, valid_order):
        config = {"order_sync": {"rules": [{"mode": "python", "match": {"docnum": ["0000000042"]}}]}}
        store, creator = InMemoryIdempotencyStore(), _Recorder("0000099999")
        canary = InboundOrderMessage(message_id="0000000042", order=valid_order)
        other = InboundOrderMessage(message_id="0000000043", order=valid_order)

        routed_canary = route_order_message(
            canary, order_creator=creator, idempotency_store=store, flags_config=config
        )
        routed_other = route_order_message(
            other, legacy_handler=_abap_created, order_creator=creator,
            idempotency_store=store, flags_config=config,
        )

        assert routed_canary.served_by == "python"
        assert routed_other.served_by == "abap"
        assert creator.calls == 1

    def test_route_order_message_targets_sender_partner(self, valid_message):
        env = {"FF_ORDER_SYNC_MODE": "python", "FF_ORDER_SYNC_SNDPRN": "EDI-GATEWAY"}
        routed = route_order_message(
            valid_message, order_creator=_Recorder("0000099999"),
            idempotency_store=InMemoryIdempotencyStore(), env=env,
        )
        assert routed.served_by == "python"


class TestRouteInboundIdoc:

    def test_route_inbound_idoc_validate_only_parses_segments(self, sample_idoc_segments):
        creator = _Recorder("0000099999")

        routed = route_inbound_idoc(
            "0000000777", sample_idoc_segments, sender_partner="EDI-GATEWAY",
            legacy_handler=_abap_created, order_creator=creator,
            idempotency_store=InMemoryIdempotencyStore(), env=VALIDATE_ONLY_ENV,
        )

        assert creator.calls == 0
        assert routed.decision.attributes["kunnr"] == "CUST-001"
        assert routed.details["would_have"]["item_count"] == 2
        assert routed.diff.clean
