"""
Tests for the Order Sync service.

Validates functional equivalence between the ABAP Z_IDOC_ORDER_SYNC
function module and the migrated Python implementation. Each test maps
to a specific piece of ABAP logic with docstrings referencing the original.
"""

from datetime import date
from decimal import Decimal

import pytest

from python_target.order_sync.models import (
    BapiReturn,
    InboundOrderMessage,
    OrderCreationResult,
    OrderHeader,
    OrderItem,
    OrderPartner,
    OrderStatus,
    PartnerRole,
)
from python_target.order_sync.service import (
    is_processable,
    map_to_bapi_payload,
    parse_idoc_to_order,
    process_order_batch,
    process_single_order,
    set_idoc_status,
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
        {"segment_type": "E1EDK01", "BSART": "ZOR", "CURCY": "USD"},
        {"segment_type": "E1EDK03", "IDDAT": "012", "DATUM": "2024-07-01"},
        {"segment_type": "E1EDK03", "IDDAT": "022", "DATUM": "2024-06-15"},
        {"segment_type": "E1EDK03", "IDDAT": "026", "DATUM": "2024-06-20"},
        {"segment_type": "E1EDK03", "IDDAT": "999", "DATUM": "2024-01-01"},
        {"segment_type": "E1EDKA1", "PARVW": "AG", "PARTN": "CUST-001"},
        {"segment_type": "E1EDKA1", "PARVW": "WE", "PARTN": "CUST-002"},
        {"segment_type": "E1EDKA1", "PARVW": "RE", "PARTN": "CUST-003"},
        {"segment_type": "E1EDKA1", "PARVW": "RG", "PARTN": "CUST-004"},
        {"segment_type": "E1EDKA1", "PARVW": "ZZ", "PARTN": "IGNORED"},
        {
            "segment_type": "E1EDP01",
            "POSEX": "000010",
            "MENGE": 100,
            "MENEE": "EA",
            "VPREI": 25.50,
            "PSTYV": "TAN",
        },
        {"segment_type": "E1EDP19", "QUALF": "002", "IDTNR": "MAT-001"},
        {"segment_type": "E1EDP19", "QUALF": "003", "IDTNR": "CUST-MAT-A"},
        {"segment_type": "E1EDP19", "QUALF": "001", "IDTNR": "IGNORED"},
        {
            "segment_type": "E1EDP01",
            "POSEX": "000020",
            "MENGE": 50,
            "MENEE": "EA",
            "VPREI": 42.00,
        },
        {"segment_type": "E1EDP19", "QUALF": "002", "IDTNR": "MAT-002"},
    ]


def _creator_with(messages: list[BapiReturn], order_number: str = "0000012345"):
    """Build a fake BAPI call returning the given BAPIRET2 messages."""

    def _create(payload):
        return OrderCreationResult(order_number=order_number, messages=messages)

    return _create


# ---------------------------------------------------------------------------
# validate_order — mirrors ABAP "Validate Parsed Data" block
# ---------------------------------------------------------------------------

class TestValidateOrder:

    def test_valid_order_passes(self, valid_order):
        errors = validate_order(valid_order)
        assert errors == []

    def test_missing_sold_to(self, valid_order):
        """ABAP: IF ls_header-sold_to IS INITIAL → '51' 'E' 'Missing sold-to party in IDoc'."""
        valid_order.sold_to_party = ""
        assert validate_order(valid_order) == ["Missing sold-to party in IDoc"]

    def test_no_items(self, valid_order):
        """ABAP: IF lt_items IS INITIAL → '51' 'E' 'No order items found in IDoc'."""
        valid_order.items = []
        assert validate_order(valid_order) == ["No order items found in IDoc"]

    def test_sold_to_checked_before_items(self, valid_order):
        """ABAP: sold-to check CONTINUEs before the items check runs."""
        valid_order.sold_to_party = ""
        valid_order.items = []
        assert validate_order(valid_order) == ["Missing sold-to party in IDoc"]

    def test_no_extra_checks_beyond_abap(self, valid_order):
        """ABAP performs no sales-org / quantity / material validation."""
        valid_order.sales_org = ""
        valid_order.distribution_channel = ""
        valid_order.division = ""
        valid_order.items[0].quantity = Decimal("0")
        valid_order.items[0].material_number = None
        valid_order.items[0].customer_material = None
        assert validate_order(valid_order) == []


# ---------------------------------------------------------------------------
# parse_idoc_to_order — mirrors ABAP segment parsing CASE/WHEN logic
# ---------------------------------------------------------------------------

class TestParseIdocToOrder:

    def test_header_fields(self, sample_idoc_segments):
        """ABAP: WHEN 'E1EDK01' → doc_type = bsart, currency = curcy (nothing else)."""
        order = parse_idoc_to_order(sample_idoc_segments)
        assert order.document_type == "ZOR"
        assert order.currency == "USD"
        assert order.sales_org == ""
        assert order.distribution_channel == ""
        assert order.division == ""

    def test_date_parsing(self, sample_idoc_segments):
        """ABAP: WHEN 'E1EDK03' CASE iddat 012/022/026; other qualifiers ignored."""
        order = parse_idoc_to_order(sample_idoc_segments)
        assert order.requested_delivery_date == date(2024, 7, 1)
        assert order.customer_po_date == date(2024, 6, 15)
        assert order.pricing_date == date(2024, 6, 20)

    def test_partner_parsing(self, sample_idoc_segments):
        """ABAP: WHEN 'E1EDKA1' CASE parvw AG/WE/RE/RG; unknown → CLEAR, not appended."""
        order = parse_idoc_to_order(sample_idoc_segments)
        assert order.sold_to_party == "CUST-001"
        assert order.ship_to_party == "CUST-002"
        assert [(p.role, p.number) for p in order.partners] == [
            (PartnerRole.SOLD_TO, "CUST-001"),
            (PartnerRole.SHIP_TO, "CUST-002"),
            (PartnerRole.BILL_TO, "CUST-003"),
            (PartnerRole.PAYER, "CUST-004"),
        ]

    def test_item_parsing(self, sample_idoc_segments):
        """ABAP: WHEN 'E1EDP01' → posex, menge, menee, vprei, pstyv."""
        order = parse_idoc_to_order(sample_idoc_segments)
        assert len(order.items) == 2
        first = order.items[0]
        assert first.item_number == "000010"
        assert first.quantity == Decimal("100")
        assert first.unit_of_measure == "EA"
        assert first.net_price == Decimal("25.5")
        assert first.item_category == "TAN"
        assert first.plant is None
        assert order.items[1].item_number == "000020"

    def test_initial_item_segment_not_appended(self):
        """ABAP: IF ls_item IS NOT INITIAL. APPEND ls_item TO lt_items."""
        order = parse_idoc_to_order([{"segment_type": "E1EDP01"}])
        assert order.items == []

    def test_material_identification(self, sample_idoc_segments):
        """ABAP: WHEN 'E1EDP19' CASE qualf 002=material, 003=cust_mat; applied to last item."""
        order = parse_idoc_to_order(sample_idoc_segments)
        assert order.items[0].material_number == "MAT-001"
        assert order.items[0].customer_material == "CUST-MAT-A"
        assert order.items[1].material_number == "MAT-002"
        assert order.items[1].customer_material is None

    def test_material_segment_before_any_item_is_ignored(self):
        """ABAP: WHEN 'E1EDP19'. IF lt_items IS NOT INITIAL ... (else nothing)."""
        order = parse_idoc_to_order(
            [{"segment_type": "E1EDP19", "QUALF": "002", "IDTNR": "MAT-X"}]
        )
        assert order.items == []

    def test_unknown_segment_ignored(self):
        order = parse_idoc_to_order([{"segment_type": "E1EDK14", "QUALF": "008"}])
        assert order == OrderHeader()


# ---------------------------------------------------------------------------
# map_to_bapi_payload — mirrors ABAP "Map to BAPI Structures" block
# ---------------------------------------------------------------------------

class TestMapToBapiPayload:

    def test_default_doc_type(self, valid_order):
        """ABAP: IF ls_order_header_in-doc_type IS INITIAL. doc_type = 'ZOR'."""
        valid_order.document_type = ""
        payload = map_to_bapi_payload(valid_order)
        assert payload.header.document_type == "ZOR"

    def test_explicit_doc_type_kept(self, valid_order):
        valid_order.document_type = "ZRE"
        payload = map_to_bapi_payload(valid_order)
        assert payload.header.document_type == "ZRE"

    def test_header_flags(self, valid_order):
        """ABAP: ls_order_header_inx-doc_type/sales_org/distr_chan/division/purch_no = 'X', updateflag = 'I'."""
        payload = map_to_bapi_payload(valid_order)
        assert payload.header_flags == {
            "doc_type": "X",
            "sales_org": "X",
            "distr_chan": "X",
            "division": "X",
            "purch_no": "X",
            "updateflag": "I",
        }

    def test_schedule_lines(self, valid_order):
        """ABAP: one BAPISCHDL per item: sched_line '0001', req_date = header req_dlv_date, req_qty = quantity."""
        payload = map_to_bapi_payload(valid_order)
        assert [
            (s.item_number, s.schedule_line, s.requested_date, s.requested_quantity)
            for s in payload.schedules
        ] == [
            ("000010", "0001", date(2024, 7, 1), Decimal("100")),
            ("000020", "0001", date(2024, 7, 1), Decimal("50")),
        ]
        assert payload.partners == valid_order.partners


# ---------------------------------------------------------------------------
# set_idoc_status — mirrors ABAP FORM set_idoc_status
# ---------------------------------------------------------------------------

class TestSetIdocStatus:

    def test_short_message(self):
        """ABAP: msgv1 = iv_msgv1(50); msgv2 only when strlen > 50."""
        result = set_idoc_status("MSG", "53", "S", "Sales order 0000012345 created")
        assert result.message_v1 == "Sales order 0000012345 created"
        assert result.message_v2 == ""

    def test_long_message_split(self):
        """ABAP: msgv1 = iv_msgv1(50). msgv2 = iv_msgv1+50(50)."""
        msg = "A" * 50 + "B" * 50 + "C" * 20
        result = set_idoc_status("MSG", "51", "E", msg)
        assert result.message_v1 == "A" * 50
        assert result.message_v2 == "B" * 50


# ---------------------------------------------------------------------------
# process_single_order — mirrors ABAP per-IDoc processing loop body
# ---------------------------------------------------------------------------

class TestProcessSingleOrder:

    def test_successful_order_creation(self, valid_message):
        """ABAP: BAPI returns no E/A → BAPI_TRANSACTION_COMMIT → status '53' 'S'."""
        result = process_single_order(valid_message)

        assert result.status == OrderStatus.CREATED
        assert result.idoc_status == "53"
        assert result.message_type == "S"
        assert result.order_number is not None
        assert len(result.order_number) == 10  # SAP VBELN format
        assert result.message_v1 == f"Sales order {result.order_number} created"
        assert result.error_messages == []

    def test_validation_failure(self, valid_message):
        """ABAP: Validation fails → set_idoc_status '51' 'E' 'Missing sold-to party in IDoc'."""
        valid_message.order.sold_to_party = ""
        result = process_single_order(valid_message)

        assert result.status == OrderStatus.FAILED
        assert result.idoc_status == "51"
        assert result.message_type == "E"
        assert result.message_v1 == "Missing sold-to party in IDoc"
        assert result.order_number is None

    def test_bapi_error_triggers_rollback(self, valid_message):
        """ABAP: LOOP AT lt_return WHERE type CA 'EA' → ROLLBACK → '51' 'E' with joined messages."""
        creator = _creator_with(
            [
                BapiReturn(type="S", message="Info only"),
                BapiReturn(type="E", message="Material MAT-001 not found"),
                BapiReturn(type="W", message="Warning ignored"),
                BapiReturn(type="A", message="Abort"),
            ]
        )
        result = process_single_order(valid_message, create_order=creator)

        assert result.status == OrderStatus.FAILED
        assert result.idoc_status == "51"
        assert result.message_type == "E"
        assert result.order_number is None
        assert result.error_messages == ["Material MAT-001 not found", "Abort"]
        # REDUCE: |{ msg }{ wa-message }; |
        assert result.message_v1 == "Material MAT-001 not found; Abort; "

    def test_bapi_warnings_do_not_block_commit(self, valid_message):
        """ABAP: type CA 'EA' only — W/I/S messages still commit."""
        creator = _creator_with(
            [BapiReturn(type="W", message="Price date adjusted")],
            order_number="0000099999",
        )
        result = process_single_order(valid_message, create_order=creator)

        assert result.status == OrderStatus.CREATED
        assert result.order_number == "0000099999"
        assert result.message_v1 == "Sales order 0000099999 created"


# ---------------------------------------------------------------------------
# process_order_batch — mirrors ABAP LOOP AT idoc_contrl
# ---------------------------------------------------------------------------

class TestProcessOrderBatch:

    def test_mixed_batch(self, valid_order):
        """Process batch with mix of valid and invalid orders."""
        messages = [
            InboundOrderMessage(message_id="MSG-001", order=valid_order),
            InboundOrderMessage(
                message_id="MSG-002",
                order=OrderHeader(sold_to_party="", items=[]),
            ),
            InboundOrderMessage(message_id="MSG-003", order=valid_order),
        ]

        batch_result = process_order_batch(messages)

        assert batch_result.total_processed == 3
        assert batch_result.successful == 2
        assert batch_result.failed == 1
        assert batch_result.skipped == 0
        assert batch_result.results[1].status == OrderStatus.FAILED
        assert batch_result.results[1].message_v1 == "Missing sold-to party in IDoc"

    def test_control_record_filter(self, valid_order):
        """ABAP: LOOP AT idoc_contrl WHERE mestyp = 'ORDERS' AND status = '64'."""
        messages = [
            InboundOrderMessage(message_id="OK", order=valid_order),
            InboundOrderMessage(
                message_id="WRONG-TYPE", message_type="INVOIC", order=valid_order
            ),
            InboundOrderMessage(message_id="WRONG-STATUS", status="53", order=valid_order),
        ]
        assert is_processable(messages[0])
        assert not is_processable(messages[1])
        assert not is_processable(messages[2])

        batch_result = process_order_batch(messages)
        assert batch_result.total_processed == 1
        assert batch_result.skipped == 2
        assert [r.message_id for r in batch_result.results] == ["OK"]

    def test_batch_failure_isolated_per_idoc(self, valid_order):
        """ABAP: rollback for one IDoc does not affect the others in the loop."""
        calls = {"n": 0}

        def flaky(payload):
            calls["n"] += 1
            if calls["n"] == 1:
                return OrderCreationResult(messages=[BapiReturn(type="E", message="boom")])
            return OrderCreationResult(order_number="0000000002")

        messages = [
            InboundOrderMessage(message_id="A", order=valid_order),
            InboundOrderMessage(message_id="B", order=valid_order),
        ]
        batch_result = process_order_batch(messages, create_order=flaky)
        assert [r.status for r in batch_result.results] == [
            OrderStatus.FAILED,
            OrderStatus.CREATED,
        ]

    def test_empty_batch(self):
        batch_result = process_order_batch([])
        assert batch_result.total_processed == 0
        assert batch_result.successful == 0
        assert batch_result.failed == 0
