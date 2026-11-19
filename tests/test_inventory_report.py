"""
Tests for the Inventory Report service.

Validates functional equivalence between the ABAP Z_INVENTORY_REPORT
and the migrated Python implementation. Each test maps to a specific
piece of ABAP logic with comments referencing the original code.
"""

from datetime import date, timedelta
from decimal import Decimal
import logging

import pytest

from python_target.feature_flags import (
    InventoryReportFlagMode,
    LegacyHandlerRequired,
)
from python_target.inventory_report import service as inventory_service
from python_target.inventory_report.models import (
    InventoryFilters,
    InventoryItem,
    InventoryReportSummary,
    StockStatus,
)
from python_target.inventory_report.service import (
    build_summary,
    calculate_stock_percentage,
    calculate_stock_status,
    filter_by_status,
    generate_inventory_report,
    route_inventory_report,
)


# ---------------------------------------------------------------------------
# calculate_stock_status — mirrors ABAP FORM calculate_stock_status
# ---------------------------------------------------------------------------

class TestCalculateStockStatus:
    """Tests for traffic-light logic matching ABAP thresholds."""

    def test_zero_stock_is_critical(self):
        """ABAP: IF labst <= 0. stock_status = '1'."""
        assert calculate_stock_status(
            available_stock=Decimal("0"),
            reorder_point=Decimal("100"),
            last_receipt_date=date.today(),
            stale_days=90,
        ) == StockStatus.CRITICAL

    def test_below_reorder_is_critical(self):
        """ABAP: ELSEIF labst < minbe. stock_status = '1'."""
        assert calculate_stock_status(
            available_stock=Decimal("80"),
            reorder_point=Decimal("100"),
            last_receipt_date=date.today(),
            stale_days=90,
        ) == StockStatus.CRITICAL

    def test_between_reorder_and_1_5x_is_warning(self):
        """ABAP: ELSEIF labst < ( minbe * '1.5' ). stock_status = '2'."""
        assert calculate_stock_status(
            available_stock=Decimal("120"),
            reorder_point=Decimal("100"),
            last_receipt_date=date.today(),
            stale_days=90,
        ) == StockStatus.WARNING

    def test_above_1_5x_reorder_is_healthy(self):
        """ABAP: ELSE. stock_status = '3'."""
        assert calculate_stock_status(
            available_stock=Decimal("200"),
            reorder_point=Decimal("100"),
            last_receipt_date=date.today(),
            stale_days=90,
        ) == StockStatus.HEALTHY

    def test_stale_stock_downgrades_healthy_to_warning(self):
        """ABAP: IF lv_age > p_stale AND stock_status = '3'. stock_status = '2'."""
        old_receipt = date.today() - timedelta(days=120)
        assert calculate_stock_status(
            available_stock=Decimal("200"),
            reorder_point=Decimal("100"),
            last_receipt_date=old_receipt,
            stale_days=90,
        ) == StockStatus.WARNING

    def test_stale_stock_does_not_downgrade_critical(self):
        """Stale override only applies to HEALTHY status."""
        old_receipt = date.today() - timedelta(days=120)
        assert calculate_stock_status(
            available_stock=Decimal("50"),
            reorder_point=Decimal("100"),
            last_receipt_date=old_receipt,
            stale_days=90,
        ) == StockStatus.CRITICAL

    def test_no_reorder_point_defaults_healthy(self):
        """ABAP: IF minbe > 0 ... ELSE stock_pct = 100 (implies healthy)."""
        assert calculate_stock_status(
            available_stock=Decimal("10"),
            reorder_point=Decimal("0"),
            last_receipt_date=date.today(),
            stale_days=90,
        ) == StockStatus.HEALTHY

    def test_no_receipt_date_stays_healthy(self):
        """No last receipt date means stale check is skipped."""
        assert calculate_stock_status(
            available_stock=Decimal("200"),
            reorder_point=Decimal("100"),
            last_receipt_date=None,
            stale_days=90,
        ) == StockStatus.HEALTHY


# ---------------------------------------------------------------------------
# calculate_stock_percentage — mirrors ABAP percentage calculation
# ---------------------------------------------------------------------------

class TestCalculateStockPercentage:

    def test_normal_percentage(self):
        """ABAP: stock_pct = ( labst / minbe ) * 100."""
        result = calculate_stock_percentage(Decimal("150"), Decimal("100"))
        assert result == Decimal("150")

    def test_zero_reorder_returns_100(self):
        """ABAP: IF minbe > 0 ... ELSE stock_pct = 100."""
        result = calculate_stock_percentage(Decimal("50"), Decimal("0"))
        assert result == Decimal("100")

    def test_below_reorder(self):
        result = calculate_stock_percentage(Decimal("30"), Decimal("100"))
        assert result == Decimal("30")


# ---------------------------------------------------------------------------
# filter_by_status — mirrors ABAP FORM filter_by_status
# ---------------------------------------------------------------------------

class TestFilterByStatus:

    @pytest.fixture()
    def sample_items(self) -> list[InventoryItem]:
        def make_item(status: StockStatus) -> InventoryItem:
            return InventoryItem(
                material_number="MAT-001",
                description="Test Material",
                material_type="FERT",
                material_group="001",
                plant="1000",
                storage_location="0001",
                available_stock=Decimal("100"),
                total_stock=Decimal("100"),
                reorder_point=Decimal("50"),
                stock_status=status,
                stock_percentage=Decimal("200"),
                stock_value=Decimal("1000"),
            )

        return [
            make_item(StockStatus.CRITICAL),
            make_item(StockStatus.WARNING),
            make_item(StockStatus.HEALTHY),
        ]

    def test_show_only_critical(self, sample_items):
        """ABAP: WHEN '1'. IF p_red = 'X'. APPEND."""
        filters = InventoryFilters(
            show_critical=True, show_warning=False, show_healthy=False
        )
        result = filter_by_status(sample_items, filters)
        assert len(result) == 1
        assert result[0].stock_status == StockStatus.CRITICAL

    def test_show_critical_and_warning(self, sample_items):
        """Default filter settings from ABAP selection screen."""
        filters = InventoryFilters(
            show_critical=True, show_warning=True, show_healthy=False
        )
        result = filter_by_status(sample_items, filters)
        assert len(result) == 2

    def test_show_all(self, sample_items):
        filters = InventoryFilters(
            show_critical=True, show_warning=True, show_healthy=True
        )
        result = filter_by_status(sample_items, filters)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# generate_inventory_report — end-to-end integration test
# ---------------------------------------------------------------------------

class TestGenerateInventoryReport:

    def test_full_report_generation(self):
        """End-to-end test matching the full ABAP START-OF-SELECTION flow."""
        raw_data = [
            {
                "material_number": "MAT-001",
                "description": "Widget A",
                "material_type": "FERT",
                "material_group": "001",
                "plant": "1000",
                "storage_location": "0001",
                "available_stock": 200,
                "inspection_stock": 10,
                "blocked_stock": 5,
                "reorder_point": 100,
                "max_stock_level": 500,
                "last_receipt_date": date.today().isoformat(),
                "unit_cost": 25,
            },
            {
                "material_number": "MAT-002",
                "description": "Widget B",
                "material_type": "FERT",
                "material_group": "001",
                "plant": "1000",
                "storage_location": "0001",
                "available_stock": 30,
                "reorder_point": 100,
                "last_receipt_date": date.today().isoformat(),
                "unit_cost": 50,
            },
            {
                "material_number": "MAT-003",
                "description": "Widget C",
                "material_type": "FERT",
                "material_group": "002",
                "plant": "2000",
                "storage_location": "0001",
                "available_stock": 0,
                "reorder_point": 50,
                "unit_cost": 15,
            },
        ]

        filters = InventoryFilters(
            show_critical=True, show_warning=True, show_healthy=True
        )

        report = generate_inventory_report(raw_data, filters)

        assert report.total_items == 3
        assert report.summary.critical_count >= 1
        # Items sorted: critical first, then by plant
        assert report.items[0].stock_status == StockStatus.CRITICAL

    def test_report_filters_correctly(self):
        """Verify status filter excludes healthy items by default."""
        raw_data = [
            {
                "material_number": "MAT-001",
                "description": "Healthy Widget",
                "plant": "1000",
                "available_stock": 500,
                "reorder_point": 100,
                "last_receipt_date": date.today().isoformat(),
            },
        ]

        # Default filters: show critical + warning only
        filters = InventoryFilters()
        report = generate_inventory_report(raw_data, filters)

        # Healthy item should be excluded
        assert report.total_items == 0

    def test_report_with_empty_data(self):
        """Matches ABAP: IF sy-subrc <> 0 → empty result."""
        filters = InventoryFilters(
            show_critical=True, show_warning=True, show_healthy=True
        )
        report = generate_inventory_report([], filters)
        assert report.total_items == 0
        assert report.items == []


# ---------------------------------------------------------------------------
# route_inventory_report — feature-flag routing (abap → shadow → python)
# ---------------------------------------------------------------------------

FLAG_LOGGER = "python_target.feature_flags"


@pytest.fixture()
def routing_raw_data() -> list[dict]:
    today = date.today()
    return [
        {"material_number": "MAT-001", "plant": "1000", "storage_location": "0001",
         "available_stock": 200, "reorder_point": 100, "last_receipt_date": today.isoformat()},
        {"material_number": "MAT-002", "plant": "1000", "storage_location": "0001",
         "available_stock": 200, "reorder_point": 100,
         "last_receipt_date": (today - timedelta(days=120)).isoformat()},
        {"material_number": "MAT-003", "plant": "1000", "storage_location": "0001",
         "available_stock": 80, "reorder_point": 100, "last_receipt_date": today.isoformat()},
    ]


@pytest.fixture()
def routing_filters() -> InventoryFilters:
    return InventoryFilters(plants=["1000"], show_critical=True, show_warning=True, show_healthy=True)


def _recorded_baseline(raw_data, filters):
    """Stands in for the ABAP ALV output; identical to Python unless mutated."""
    return generate_inventory_report(raw_data, filters)


class TestRouteInventoryReport:

    def test_diff_inventory_reports_rounds_stock_percentage_half_up(
        self, routing_raw_data, routing_filters
    ):
        report = generate_inventory_report(routing_raw_data, routing_filters)
        item = report.items[0]
        legacy = report.model_copy(update={
            "items": [item.model_copy(update={"stock_percentage": Decimal("100.01")})]
        })
        python = report.model_copy(update={
            "items": [item.model_copy(update={"stock_percentage": Decimal("100.005")})]
        })

        assert inventory_service.diff_inventory_reports(legacy, python).clean

    def test_route_inventory_report_defaults_to_abap(self, routing_raw_data, routing_filters, monkeypatch):
        legacy = _recorded_baseline(routing_raw_data, routing_filters)
        monkeypatch.setattr(inventory_service, "generate_inventory_report", _fail_if_called)

        routed = route_inventory_report(
            routing_raw_data, routing_filters, legacy_handler=lambda *_: legacy, env={}
        )

        assert routed.decision.mode == InventoryReportFlagMode.ABAP
        assert routed.served_by == "abap"
        assert routed.result is legacy
        assert routed.diff is None

    def test_route_inventory_report_shadow_returns_legacy_and_logs_diff(
        self, routing_raw_data, routing_filters, caplog
    ):
        legacy = _recorded_baseline(routing_raw_data, routing_filters)
        mat1 = next(i for i in legacy.items if i.material_number == "MAT-001")
        mat1.stock_status = StockStatus.WARNING
        mat1.stock_percentage = Decimal("199.5")

        with caplog.at_level(logging.INFO, logger=FLAG_LOGGER):
            routed = route_inventory_report(
                routing_raw_data, routing_filters,
                legacy_handler=lambda *_: legacy,
                env={"FF_INVENTORY_REPORT_MODE": "shadow"},
            )

        assert routed.result is legacy
        assert routed.served_by == "abap"
        fields = {(d.key, d.field) for d in routed.diff.differences}
        assert fields == {
            ("MAT-001/1000/0001", "stock_status"),
            ("MAT-001/1000/0001", "stock_percentage"),
            ("MAT-001/1000/0001", "stale_downgrade"),
        }
        event = caplog.records[-1].flag_event
        assert event["mode"] == "shadow"
        assert event["diff"]["clean"] is False
        assert event["diff"]["difference_count"] == 3

    def test_route_inventory_report_shadow_clean_when_paths_agree(
        self, routing_raw_data, routing_filters, caplog
    ):
        with caplog.at_level(logging.INFO, logger=FLAG_LOGGER):
            routed = route_inventory_report(
                routing_raw_data, routing_filters,
                legacy_handler=_recorded_baseline,
                env={"FF_INVENTORY_REPORT_MODE": "shadow"},
            )

        assert routed.diff.clean
        assert caplog.records[-1].flag_event["diff"]["clean"] is True

    def test_route_inventory_report_shadow_detects_missing_stale_downgrade(
        self, routing_raw_data, routing_filters
    ):
        legacy = _recorded_baseline(routing_raw_data, routing_filters)
        stale = next(i for i in legacy.items if i.material_number == "MAT-002")
        assert inventory_service.is_stale_downgrade(stale)
        stale.stock_status = StockStatus.HEALTHY

        routed = route_inventory_report(
            routing_raw_data, routing_filters,
            legacy_handler=lambda *_: legacy,
            env={"FF_INVENTORY_REPORT_MODE": "shadow"},
        )

        diffs = {d.field: d for d in routed.diff.differences}
        assert diffs["stale_downgrade"].legacy is False
        assert diffs["stale_downgrade"].python is True

    def test_route_inventory_report_shadow_survives_python_error(
        self, routing_raw_data, routing_filters, monkeypatch
    ):
        legacy = _recorded_baseline(routing_raw_data, routing_filters)
        monkeypatch.setattr(inventory_service, "generate_inventory_report", _fail_if_called)

        routed = route_inventory_report(
            routing_raw_data, routing_filters,
            legacy_handler=lambda *_: legacy,
            env={"FF_INVENTORY_REPORT_MODE": "shadow"},
        )

        assert routed.result is legacy
        assert "python path called" in routed.diff.python_error

    def test_route_inventory_report_python_mode_returns_python_result(
        self, routing_raw_data, routing_filters
    ):
        routed = route_inventory_report(
            routing_raw_data, routing_filters,
            legacy_handler=_fail_if_called,
            env={"FF_INVENTORY_REPORT_MODE": "python"},
        )

        assert routed.served_by == "python"
        assert routed.result.total_items == 3

    def test_route_inventory_report_targets_only_cohort_plants(self, routing_raw_data):
        config = {"inventory_report": {"rules": [{"mode": "python", "match": {"werks": ["1000"]}}]}}
        legacy = _recorded_baseline(routing_raw_data, InventoryFilters())

        canary = route_inventory_report(
            routing_raw_data, InventoryFilters(plants=["1000"]), flags_config=config
        )
        mixed = route_inventory_report(
            routing_raw_data, InventoryFilters(plants=["1000", "2000"]),
            legacy_handler=lambda *_: legacy, flags_config=config,
        )

        assert canary.served_by == "python"
        assert mixed.served_by == "abap"

    def test_route_inventory_report_requires_legacy_handler(self, routing_raw_data, routing_filters):
        with pytest.raises(LegacyHandlerRequired):
            route_inventory_report(routing_raw_data, routing_filters, env={})


def _fail_if_called(*_args, **_kwargs):
    raise AssertionError("python path called")
