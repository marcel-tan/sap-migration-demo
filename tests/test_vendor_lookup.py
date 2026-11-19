"""
Tests for the Vendor Lookup service.

Validates functional equivalence between the ABAP Z_RFC_VENDOR_LOOKUP
function module and the migrated Python implementation.
"""

from datetime import date, timedelta
from decimal import Decimal
import logging

import pytest

from python_target.feature_flags import LegacyHandlerRequired, VendorLookupFlagMode
from python_target.vendor_lookup.models import (
    PurchaseOrderItem,
    VendorLookupRequest,
)
from python_target.vendor_lookup.service import (
    AuthorizationError,
    VendorNotFoundError,
    calculate_po_aggregates,
    lookup_vendor,
    route_vendor_lookup,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_vendor_data() -> dict:
    """Vendor master record — represents joined LFA1 + LFB1 + ADR6 data."""
    return {
        "vendor_number": "0000001000",
        "name1": "Acme Supplies Inc.",
        "name2": "Eastern Division",
        "street": "123 Industrial Pkwy",
        "city": "Springfield",
        "region": "IL",
        "postal_code": "62701",
        "country": "US",
        "phone": "+1-217-555-0100",
        "fax": "+1-217-555-0101",
        "email": "orders@acme-supplies.example.com",
        "account_group": "LIEF",
        "payment_terms": "ZB30",
        "recon_account": "160000",
        "currency": "USD",
        "is_blocked": False,
        "is_deleted": False,
    }


@pytest.fixture()
def sample_po_data() -> list[dict]:
    """Purchase order items — represents EKKO + EKPO join."""
    today = date.today()
    return [
        {
            "po_number": "4500001234",
            "po_item": "00010",
            "po_date": (today - timedelta(days=30)).isoformat(),
            "material_number": "MAT-001",
            "short_text": "Widget A - Standard",
            "quantity": 100,
            "unit_of_measure": "EA",
            "net_price": 25.50,
            "currency": "USD",
            "delivery_completed": False,
            "purchase_requisition": "1000001234",
        },
        {
            "po_number": "4500001234",
            "po_item": "00020",
            "po_date": (today - timedelta(days=30)).isoformat(),
            "material_number": "MAT-002",
            "short_text": "Widget B - Premium",
            "quantity": 50,
            "unit_of_measure": "EA",
            "net_price": 42.00,
            "currency": "USD",
            "delivery_completed": True,
            "purchase_requisition": "1000001234",
        },
        {
            "po_number": "4500001100",
            "po_item": "00010",
            "po_date": (today - timedelta(days=90)).isoformat(),
            "material_number": "MAT-003",
            "short_text": "Gizmo C - Bulk",
            "quantity": 500,
            "unit_of_measure": "KG",
            "net_price": 8.75,
            "currency": "USD",
            "delivery_completed": True,
        },
    ]


# ---------------------------------------------------------------------------
# calculate_po_aggregates — mirrors ABAP LOOP aggregation
# ---------------------------------------------------------------------------

class TestCalculatePOAggregates:

    def test_total_value_calculation(self):
        """ABAP: lv_total = lv_total + ( ls_po-netpr * ls_po-menge )."""
        items = [
            PurchaseOrderItem(
                po_number="4500001234",
                po_item="00010",
                po_date=date.today(),
                quantity=Decimal("100"),
                unit_of_measure="EA",
                net_price=Decimal("25.50"),
                delivery_completed=False,
            ),
            PurchaseOrderItem(
                po_number="4500001234",
                po_item="00020",
                po_date=date.today(),
                quantity=Decimal("50"),
                unit_of_measure="EA",
                net_price=Decimal("42.00"),
                delivery_completed=True,
            ),
        ]

        total_value, open_count = calculate_po_aggregates(items)

        # 100 * 25.50 + 50 * 42.00 = 2550 + 2100 = 4650
        assert total_value == Decimal("4650.00")

    def test_open_po_count(self):
        """ABAP: IF ls_po-elikz = ' '. lv_open = lv_open + 1."""
        items = [
            PurchaseOrderItem(
                po_number="PO1",
                po_item="10",
                po_date=date.today(),
                quantity=Decimal("10"),
                unit_of_measure="EA",
                net_price=Decimal("5"),
                delivery_completed=False,  # Open
            ),
            PurchaseOrderItem(
                po_number="PO2",
                po_item="10",
                po_date=date.today(),
                quantity=Decimal("20"),
                unit_of_measure="EA",
                net_price=Decimal("10"),
                delivery_completed=True,  # Closed
            ),
        ]

        _, open_count = calculate_po_aggregates(items)
        assert open_count == 1

    def test_empty_po_list(self):
        total_value, open_count = calculate_po_aggregates([])
        assert total_value == Decimal("0")
        assert open_count == 0


# ---------------------------------------------------------------------------
# lookup_vendor — end-to-end function module logic
# ---------------------------------------------------------------------------

class TestLookupVendor:

    def test_successful_lookup(self, sample_vendor_data, sample_po_data):
        """Matches ABAP: ev_return_code = 0, successful retrieval."""
        request = VendorLookupRequest(vendor_number="0000001000")
        response = lookup_vendor(request, sample_vendor_data, sample_po_data)

        assert response.return_code == 0
        assert response.vendor.vendor_number == "0000001000"
        assert response.vendor.name1 == "Acme Supplies Inc."
        assert len(response.po_history) == 3
        assert response.vendor.total_po_value > 0
        assert "successfully" in response.return_message

    def test_vendor_not_found(self):
        """ABAP: IF sy-subrc <> 0. RAISE vendor_not_found."""
        request = VendorLookupRequest(vendor_number="9999999999")

        with pytest.raises(VendorNotFoundError) as exc_info:
            lookup_vendor(request, None, [])

        assert "9999999999" in str(exc_info.value)

    def test_po_date_filtering(self, sample_vendor_data, sample_po_data):
        """ABAP: WHERE h~bedat >= lv_date_from."""
        # Only include POs from the last 7 days (should exclude all sample POs)
        request = VendorLookupRequest(
            vendor_number="0000001000",
            date_from=date.today() - timedelta(days=7),
        )
        response = lookup_vendor(request, sample_vendor_data, sample_po_data)

        assert response.return_code == 0
        assert len(response.po_history) == 0

    def test_po_limit(self, sample_vendor_data, sample_po_data):
        """ABAP: UP TO iv_max_pos ROWS."""
        request = VendorLookupRequest(
            vendor_number="0000001000",
            max_po_items=2,
        )
        response = lookup_vendor(request, sample_vendor_data, sample_po_data)

        assert len(response.po_history) <= 2

    def test_po_sorted_descending(self, sample_vendor_data, sample_po_data):
        """ABAP: ORDER BY h~bedat DESCENDING."""
        request = VendorLookupRequest(vendor_number="0000001000")
        response = lookup_vendor(request, sample_vendor_data, sample_po_data)

        dates = [item.po_date for item in response.po_history]
        assert dates == sorted(dates, reverse=True)

    def test_blocked_vendor_still_returned(self, sample_vendor_data):
        """Vendor with central block is returned (block is informational)."""
        sample_vendor_data["is_blocked"] = True
        request = VendorLookupRequest(vendor_number="0000001000")
        response = lookup_vendor(request, sample_vendor_data, [])

        assert response.vendor.is_blocked is True
        assert response.return_code == 0


# ---------------------------------------------------------------------------
# route_vendor_lookup — feature-flag routing (abap → shadow → python)
# ---------------------------------------------------------------------------

SHADOW_ENV = {"FF_VENDOR_LOOKUP_MODE": "shadow"}


def _fail_if_called(*_args, **_kwargs):
    raise AssertionError("path should not be called")


class TestRouteVendorLookup:

    def test_route_vendor_lookup_defaults_to_abap(self, sample_vendor_data, sample_po_data):
        request = VendorLookupRequest(vendor_number="0000001000")
        legacy = lookup_vendor(request, sample_vendor_data, sample_po_data)

        routed = route_vendor_lookup(
            request, None, [], legacy_handler=lambda _req: legacy, env={}
        )

        assert routed.decision.mode == VendorLookupFlagMode.ABAP
        assert routed.result is legacy
        assert routed.diff is None

    def test_route_vendor_lookup_shadow_returns_legacy_and_diffs_es_vendor(
        self, sample_vendor_data, sample_po_data, caplog
    ):
        request = VendorLookupRequest(vendor_number="0000001000")
        legacy = lookup_vendor(request, sample_vendor_data, sample_po_data)
        legacy.vendor.payment_terms = "ZB14"
        legacy.po_history[0].net_price = Decimal("26.00")

        with caplog.at_level(logging.INFO, logger="python_target.feature_flags"):
            routed = route_vendor_lookup(
                request, sample_vendor_data, sample_po_data,
                legacy_handler=lambda _req: legacy, env=SHADOW_ENV,
            )

        assert routed.result is legacy
        fields = {(d.key, d.field) for d in routed.diff.differences}
        assert ("ES_VENDOR", "payment_terms") in fields
        assert ("ET_PO_HISTORY/4500001234/00010", "net_price") in fields
        assert caplog.records[-1].flag_event["diff"]["clean"] is False

    def test_route_vendor_lookup_shadow_clean_when_paths_agree(
        self, sample_vendor_data, sample_po_data
    ):
        request = VendorLookupRequest(vendor_number="0000001000")
        routed = route_vendor_lookup(
            request, sample_vendor_data, sample_po_data,
            legacy_handler=lambda req: lookup_vendor(req, sample_vendor_data, sample_po_data),
            env=SHADOW_ENV,
        )

        assert routed.diff.clean
        assert routed.details["return_code"] == 0

    def test_route_vendor_lookup_shadow_vendor_not_found_reraises_legacy(self, caplog):
        request = VendorLookupRequest(vendor_number="9999999999")

        def legacy(req):
            raise VendorNotFoundError(req.vendor_number)

        with caplog.at_level(logging.INFO, logger="python_target.feature_flags"):
            with pytest.raises(VendorNotFoundError):
                route_vendor_lookup(request, None, [], legacy_handler=legacy, env=SHADOW_ENV)

        event = caplog.records[-1].flag_event
        assert event["diff"]["clean"] is True
        assert event["return_code"] == 1

    def test_route_vendor_lookup_shadow_detects_authorization_failed_mismatch(
        self, sample_vendor_data, sample_po_data, caplog
    ):
        request = VendorLookupRequest(vendor_number="0000001000", company_code="2000")

        def legacy(req):
            raise AuthorizationError(req.company_code)

        with caplog.at_level(logging.INFO, logger="python_target.feature_flags"):
            with pytest.raises(AuthorizationError):
                route_vendor_lookup(
                    request, sample_vendor_data, sample_po_data,
                    legacy_handler=legacy, auth_check=lambda _bukrs: True, env=SHADOW_ENV,
                )

        diffs = caplog.records[-1].flag_event["diff"]["differences"]
        rc = next(d for d in diffs if d["field"] == "return_code")
        assert (rc["legacy"], rc["python"]) == (2, 0)

    def test_route_vendor_lookup_shadow_authorization_failed_matches(
        self, sample_vendor_data, sample_po_data, caplog
    ):
        request = VendorLookupRequest(vendor_number="0000001000", company_code="2000")

        def legacy(req):
            raise AuthorizationError(req.company_code)

        with caplog.at_level(logging.INFO, logger="python_target.feature_flags"):
            with pytest.raises(AuthorizationError):
                route_vendor_lookup(
                    request, sample_vendor_data, sample_po_data,
                    legacy_handler=legacy, auth_check=lambda bukrs: bukrs == "1000",
                    env=SHADOW_ENV,
                )

        assert caplog.records[-1].flag_event["diff"]["clean"] is True

    def test_route_vendor_lookup_python_mode_returns_python_result(
        self, sample_vendor_data, sample_po_data
    ):
        request = VendorLookupRequest(vendor_number="0000001000")
        routed = route_vendor_lookup(
            request, sample_vendor_data, sample_po_data,
            legacy_handler=_fail_if_called, env={"FF_VENDOR_LOOKUP_MODE": "python"},
        )

        assert routed.served_by == "python"
        assert routed.result.return_code == 0
        assert len(routed.result.po_history) == 3

    def test_route_vendor_lookup_targets_bukrs_lifnr_cohort(
        self, sample_vendor_data, sample_po_data
    ):
        env = {
            "FF_VENDOR_LOOKUP_MODE": "python",
            "FF_VENDOR_LOOKUP_BUKRS": "1000",
            "FF_VENDOR_LOOKUP_LIFNR": "0000001000",
        }
        legacy = lookup_vendor(
            VendorLookupRequest(vendor_number="0000001000"), sample_vendor_data, sample_po_data
        )

        canary = route_vendor_lookup(
            VendorLookupRequest(vendor_number="0000001000", company_code="1000"),
            sample_vendor_data, sample_po_data, env=env,
        )
        other_bukrs = route_vendor_lookup(
            VendorLookupRequest(vendor_number="0000001000", company_code="2000"),
            sample_vendor_data, sample_po_data, legacy_handler=lambda _req: legacy, env=env,
        )

        assert canary.served_by == "python"
        assert other_bukrs.served_by == "abap"

    def test_route_vendor_lookup_requires_legacy_handler(self):
        with pytest.raises(LegacyHandlerRequired):
            route_vendor_lookup(VendorLookupRequest(vendor_number="1"), None, [], env={})
