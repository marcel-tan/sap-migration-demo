"""
Tests for the Inventory Report service.

Validates functional equivalence between the ABAP Z_INVENTORY_REPORT
and the migrated Python implementation. Each test maps to a specific
piece of ABAP logic with comments referencing the original code.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

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
    calculate_stock_value,
    filter_by_status,
    generate_inventory_report,
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

    def test_rounded_to_two_decimals(self):
        """ABAP: stock_pct TYPE p DECIMALS 2 (rounded, not truncated)."""
        result = calculate_stock_percentage(Decimal("1"), Decimal("3"))
        assert result == Decimal("33.33")
        result = calculate_stock_percentage(Decimal("2"), Decimal("3"))
        assert result == Decimal("66.67")


# ---------------------------------------------------------------------------
# calculate_stock_value — mirrors ABAP placeholder stock value
# ---------------------------------------------------------------------------

class TestCalculateStockValue:

    def test_placeholder_unit_cost(self):
        """ABAP: stock_value = total_stock * 10. " Placeholder unit cost."""
        assert calculate_stock_value(Decimal("215")) == Decimal("2150")
        assert calculate_stock_value(Decimal("0")) == Decimal("0")


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
        assert report.summary.critical_count == 2
        # Items sorted: critical first, then by plant
        assert report.items[0].stock_status == StockStatus.CRITICAL
        assert [i.material_number for i in report.items] == [
            "MAT-002", "MAT-003", "MAT-001"
        ]

    def test_total_stock_value_and_currency(self):
        """ABAP: total_stock = labst + insme + speme;
        stock_value = total_stock * 10; currency = 'USD' (row values ignored)."""
        raw_data = [
            {
                "material_number": "MAT-001",
                "plant": "1000",
                "available_stock": 200,
                "inspection_stock": 10,
                "blocked_stock": 5,
                "reorder_point": 100,
                "unit_cost": 25,
                "currency": "EUR",
            },
        ]
        filters = InventoryFilters(show_healthy=True)
        report = generate_inventory_report(raw_data, filters)

        item = report.items[0]
        assert item.total_stock == Decimal("215")
        assert item.stock_value == Decimal("2150")
        assert item.currency == "USD"

    def test_sort_status_then_plant(self):
        """ABAP ALV: add_sort STOCK_STATUS position 1, WERKS position 2 (both up)."""
        raw_data = [
            {"material_number": "H-2", "plant": "2000", "available_stock": 500, "reorder_point": 100},
            {"material_number": "C-3", "plant": "3000", "available_stock": 0, "reorder_point": 100},
            {"material_number": "W-1", "plant": "1000", "available_stock": 120, "reorder_point": 100},
            {"material_number": "C-1", "plant": "1000", "available_stock": 10, "reorder_point": 100},
            {"material_number": "H-1", "plant": "1000", "available_stock": 500, "reorder_point": 100},
        ]
        filters = InventoryFilters(
            show_critical=True, show_warning=True, show_healthy=True
        )
        report = generate_inventory_report(raw_data, filters)
        assert [i.material_number for i in report.items] == [
            "C-1", "C-3", "W-1", "H-1", "H-2"
        ]

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
