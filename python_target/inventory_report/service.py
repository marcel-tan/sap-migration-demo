"""
Inventory Report Service — migrated from Z_INVENTORY_REPORT.

Original ABAP: Custom ALV report reading from MARA/MAKT/MARC/MARD tables
Target:        FastAPI endpoint returning structured JSON with the same
               business logic (stock status calculation, filtering, sorting).

Migration notes:
- SAP SELECT ... INTO CORRESPONDING FIELDS → SQLAlchemy / raw SQL + pandas
- ABAP traffic-light logic → StockStatus enum with identical thresholds
- ALV grid display → JSON response (consumed by frontend dashboard)
- Selection screen → query parameters / request body
"""

from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from .models import (
    InventoryFilters,
    InventoryItem,
    InventoryReportResponse,
    InventoryReportSummary,
    StockStatus,
)

# ABAP: stock_value = total_stock * 10. " Placeholder unit cost
#       currency = 'USD'.
PLACEHOLDER_UNIT_COST = Decimal("10")
REPORT_CURRENCY = "USD"


def calculate_stock_status(
    available_stock: Decimal,
    reorder_point: Decimal,
    last_receipt_date: Optional[date],
    stale_days: int,
    today: Optional[date] = None,
) -> StockStatus:
    """Determine traffic-light stock status.

    Replicates ABAP FORM calculate_stock_status logic exactly:
    - Red:    stock <= 0 OR stock < reorder point
    - Yellow: stock < reorder point * 1.5
    - Green:  stock >= reorder point * 1.5
    - Override: downgrade green → yellow if no receipt in stale_days
    """
    if today is None:
        today = date.today()

    if available_stock <= 0:
        return StockStatus.CRITICAL
    if reorder_point > 0 and available_stock < reorder_point:
        return StockStatus.CRITICAL
    if reorder_point > 0 and available_stock < reorder_point * Decimal("1.5"):
        return StockStatus.WARNING

    status = StockStatus.HEALTHY

    # Stale stock downgrade (matches ABAP: override green → yellow)
    if last_receipt_date is not None:
        age = (today - last_receipt_date).days
        if age > stale_days and status == StockStatus.HEALTHY:
            status = StockStatus.WARNING

    return status


def calculate_stock_percentage(
    available_stock: Decimal, reorder_point: Decimal
) -> Decimal:
    """Stock level as percentage of reorder point.

    Matches ABAP: IF minbe > 0 THEN (labst / minbe) * 100 ELSE 100.
    Result is rounded to 2 decimals like the ABAP `TYPE p DECIMALS 2` field.
    """
    if reorder_point > 0:
        pct = (available_stock / reorder_point) * 100
    else:
        pct = Decimal("100")
    return pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_stock_value(total_stock: Decimal) -> Decimal:
    """Matches ABAP: stock_value = total_stock * 10 (placeholder unit cost)."""
    return total_stock * PLACEHOLDER_UNIT_COST


def filter_by_status(
    items: list[InventoryItem], filters: InventoryFilters
) -> list[InventoryItem]:
    """Apply status filter checkboxes — matches ABAP FORM filter_by_status."""
    allowed_statuses = set()
    if filters.show_critical:
        allowed_statuses.add(StockStatus.CRITICAL)
    if filters.show_warning:
        allowed_statuses.add(StockStatus.WARNING)
    if filters.show_healthy:
        allowed_statuses.add(StockStatus.HEALTHY)

    return [item for item in items if item.stock_status in allowed_statuses]


def build_summary(items: list[InventoryItem]) -> InventoryReportSummary:
    """Aggregate summary statistics — added value beyond the original ALV."""
    summary = InventoryReportSummary()
    today = date.today()

    for item in items:
        if item.stock_status == StockStatus.CRITICAL:
            summary.critical_count += 1
        elif item.stock_status == StockStatus.WARNING:
            summary.warning_count += 1
        else:
            summary.healthy_count += 1

        summary.total_stock_value += item.stock_value

        if item.reorder_point > 0 and item.available_stock < item.reorder_point:
            summary.materials_below_reorder += 1

    return summary


def generate_inventory_report(
    raw_data: list[dict],
    filters: InventoryFilters,
) -> InventoryReportResponse:
    """Main orchestrator — replaces ABAP START-OF-SELECTION event block.

    Args:
        raw_data: Rows from the data warehouse query (replaces SAP SELECT).
                  Each dict has keys matching the source table columns.
        filters:  Selection parameters from the API request.

    Returns:
        Complete report response with items, summary, and metadata.
    """
    today = date.today()
    items: list[InventoryItem] = []

    for row in raw_data:
        available = Decimal(str(row.get("available_stock", 0)))
        inspection = Decimal(str(row.get("inspection_stock", 0)))
        blocked = Decimal(str(row.get("blocked_stock", 0)))
        reorder = Decimal(str(row.get("reorder_point", 0)))
        total = available + inspection + blocked

        last_receipt = row.get("last_receipt_date")
        if isinstance(last_receipt, str):
            last_receipt = date.fromisoformat(last_receipt)

        status = calculate_stock_status(
            available_stock=available,
            reorder_point=reorder,
            last_receipt_date=last_receipt,
            stale_days=filters.stale_days,
            today=today,
        )
        pct = calculate_stock_percentage(available, reorder)

        # Stock value — simplified (production would join MBEW for moving avg price)
        stock_value = calculate_stock_value(total)

        item = InventoryItem(
            material_number=row["material_number"],
            description=row.get("description", ""),
            material_type=row.get("material_type", ""),
            material_group=row.get("material_group", ""),
            plant=row["plant"],
            storage_location=row.get("storage_location", ""),
            available_stock=available,
            inspection_stock=inspection,
            blocked_stock=blocked,
            total_stock=total,
            reorder_point=reorder,
            max_stock_level=Decimal(str(row.get("max_stock_level", 0))),
            stock_status=status,
            stock_percentage=pct,
            last_receipt_date=last_receipt,
            currency=REPORT_CURRENCY,
            stock_value=stock_value,
        )
        items.append(item)

    # Filter by status checkboxes
    items = filter_by_status(items, filters)

    # Sort: status ascending (critical first), then plant — matches ALV sort config
    status_order = {
        StockStatus.CRITICAL: 1,
        StockStatus.WARNING: 2,
        StockStatus.HEALTHY: 3,
    }
    items.sort(key=lambda x: (status_order[x.stock_status], x.plant))

    summary = build_summary(items)

    return InventoryReportResponse(
        generated_at=datetime.now(UTC),
        filters_applied=filters,
        total_items=len(items),
        summary=summary,
        items=items,
    )
