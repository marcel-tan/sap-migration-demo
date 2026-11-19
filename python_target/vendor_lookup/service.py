"""
Vendor Lookup Service — migrated from Z_RFC_VENDOR_LOOKUP.

Original ABAP: RFC-enabled function module querying LFA1/LFB1/EKKO/EKPO
Target:        FastAPI endpoint returning structured JSON.

Migration notes:
- RFC interface → REST GET endpoint with query parameters
- AUTHORITY-CHECK → API key / JWT middleware (not shown in service layer)
- SAP SELECT → data warehouse query or ORM
- ABAP RAISE exception → Python exceptions / HTTP error responses
- BAPI-style return code → HTTP status codes + response body
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, Optional

from ..feature_flags import (
    VENDOR_LOOKUP,
    FieldDiff,
    LegacyHandlerRequired,
    RoutedResult,
    ShadowDiff,
    VendorLookupFlagMode,
    emit_flag_event,
    resolve_flag,
)
from .models import (
    PurchaseOrderItem,
    VendorDetail,
    VendorLookupRequest,
    VendorLookupResponse,
)


class VendorNotFoundError(Exception):
    """Replaces ABAP RAISE vendor_not_found."""

    def __init__(self, vendor_number: str) -> None:
        self.vendor_number = vendor_number
        super().__init__(f"Vendor {vendor_number} not found")


class AuthorizationError(Exception):
    """Replaces ABAP RAISE authorization_failed."""

    def __init__(self, company_code: str) -> None:
        self.company_code = company_code
        super().__init__(f"Authorization failed for company code {company_code}")


def calculate_po_aggregates(
    po_items: list[PurchaseOrderItem],
) -> tuple[Decimal, int]:
    """Calculate total PO value and open PO count.

    Matches ABAP LOOP AT lt_po_hist aggregation logic:
      lv_total = lv_total + ( ls_po-netpr * ls_po-menge )
      IF ls_po-elikz = ' '. lv_open = lv_open + 1.
    """
    total_value = Decimal("0")
    open_count = 0

    for item in po_items:
        total_value += item.net_price * item.quantity
        if not item.delivery_completed:
            open_count += 1

    return total_value, open_count


def lookup_vendor(
    request: VendorLookupRequest,
    vendor_data: Optional[dict],
    po_data: list[dict],
) -> VendorLookupResponse:
    """Main lookup logic — replaces the ABAP function module body.

    Args:
        request:     Lookup parameters (vendor number, company code, etc.)
        vendor_data: Vendor master record from data warehouse.
                     None if vendor not found.
        po_data:     Purchase order line items from data warehouse.

    Returns:
        VendorLookupResponse with vendor details and PO history.

    Raises:
        VendorNotFoundError: If vendor_data is None.
    """
    # Vendor not found check — maps to ABAP: IF sy-subrc <> 0. RAISE vendor_not_found.
    if vendor_data is None:
        raise VendorNotFoundError(request.vendor_number)

    # Build vendor detail
    vendor = VendorDetail(
        vendor_number=vendor_data["vendor_number"],
        name1=vendor_data.get("name1", ""),
        name2=vendor_data.get("name2"),
        street=vendor_data.get("street"),
        city=vendor_data.get("city"),
        region=vendor_data.get("region"),
        postal_code=vendor_data.get("postal_code"),
        country=vendor_data.get("country"),
        phone=vendor_data.get("phone"),
        fax=vendor_data.get("fax"),
        email=vendor_data.get("email"),
        account_group=vendor_data.get("account_group"),
        payment_terms=vendor_data.get("payment_terms"),
        recon_account=vendor_data.get("recon_account"),
        currency=vendor_data.get("currency", "USD"),
        is_blocked=vendor_data.get("is_blocked", False),
        is_deleted=vendor_data.get("is_deleted", False),
    )

    # Build PO history — maps to ABAP SELECT from EKKO/EKPO
    date_from = request.date_from or (date.today() - timedelta(days=365))

    po_items: list[PurchaseOrderItem] = []
    for row in po_data:
        po_date = row.get("po_date")
        if isinstance(po_date, str):
            po_date = date.fromisoformat(po_date)

        # Apply date filter (matches ABAP: WHERE h~bedat >= lv_date_from)
        if po_date is not None and po_date < date_from:
            continue

        item = PurchaseOrderItem(
            po_number=row["po_number"],
            po_item=row["po_item"],
            po_date=po_date,
            material_number=row.get("material_number"),
            short_text=row.get("short_text"),
            quantity=Decimal(str(row.get("quantity", 0))),
            unit_of_measure=row.get("unit_of_measure", "EA"),
            net_price=Decimal(str(row.get("net_price", 0))),
            currency=row.get("currency", "USD"),
            delivery_completed=row.get("delivery_completed", False),
            purchase_requisition=row.get("purchase_requisition"),
        )
        po_items.append(item)

    # Sort by date descending and apply limit (matches ABAP: ORDER BY h~bedat DESCENDING UP TO iv_max_pos ROWS)
    po_items.sort(key=lambda x: x.po_date, reverse=True)
    po_items = po_items[: request.max_po_items]

    # Calculate aggregates
    total_value, open_count = calculate_po_aggregates(po_items)
    vendor.total_po_value = total_value
    vendor.open_po_count = open_count

    return VendorLookupResponse(
        vendor=vendor,
        po_history=po_items,
        return_code=0,
        return_message=(
            f"Vendor {request.vendor_number} retrieved successfully. "
            f"{len(po_items)} PO items returned."
        ),
    )


# ---------------------------------------------------------------------------
# Feature-flag routing (ABAP RFC → shadow → Python)
# ---------------------------------------------------------------------------

# Legacy path: calls Z_RFC_VENDOR_LOOKUP (or replays a recorded response).
# Raises VendorNotFoundError / AuthorizationError like the RFC exceptions.
VendorLegacyHandler = Callable[[VendorLookupRequest], VendorLookupResponse]

# Python-side stand-in for AUTHORITY-CHECK OBJECT 'F_LFA1_BUK'.
AuthCheck = Callable[[str], bool]

RC_OK, RC_NOT_FOUND, RC_AUTH_FAILED = 0, 1, 2


@dataclass
class _VendorOutcome:
    """Normalized RFC outcome: ES_VENDOR, ET_PO_HISTORY, return code/message."""

    return_code: int
    return_message: str
    vendor: Optional[dict[str, Any]] = None
    po_history: list[dict[str, Any]] = field(default_factory=list)
    exception: Optional[Exception] = None
    response: Optional[VendorLookupResponse] = None


def _capture(call: Callable[[], VendorLookupResponse]) -> _VendorOutcome:
    try:
        response = call()
    except VendorNotFoundError as exc:
        return _VendorOutcome(RC_NOT_FOUND, str(exc), exception=exc)
    except AuthorizationError as exc:
        return _VendorOutcome(RC_AUTH_FAILED, str(exc), exception=exc)
    return _VendorOutcome(
        response.return_code,
        response.return_message,
        vendor=response.vendor.model_dump(),
        po_history=[po.model_dump() for po in response.po_history],
        response=response,
    )


def diff_vendor_outcomes(legacy: _VendorOutcome, python: _VendorOutcome) -> ShadowDiff:
    """Compare return code/message, ES_VENDOR fields and ET_PO_HISTORY rows."""
    diff = ShadowDiff()
    for name in ("return_code", "return_message"):
        if getattr(legacy, name) != getattr(python, name):
            diff.differences.append(
                FieldDiff("RETURN", name, getattr(legacy, name), getattr(python, name))
            )

    old_vendor, new_vendor = legacy.vendor or {}, python.vendor or {}
    for name in sorted(old_vendor.keys() | new_vendor.keys()):
        if old_vendor.get(name) != new_vendor.get(name):
            diff.differences.append(
                FieldDiff("ES_VENDOR", name, old_vendor.get(name), new_vendor.get(name))
            )

    def by_key(rows: list[dict]) -> dict[str, dict]:
        return {f"{r['po_number']}/{r['po_item']}": r for r in rows}

    old_pos, new_pos = by_key(legacy.po_history), by_key(python.po_history)
    for key in sorted(old_pos.keys() | new_pos.keys()):
        old, new = old_pos.get(key), new_pos.get(key)
        if old is None or new is None:
            diff.differences.append(
                FieldDiff(f"ET_PO_HISTORY/{key}", "present", old is not None, new is not None)
            )
            continue
        for name in sorted(old.keys() | new.keys()):
            if old.get(name) != new.get(name):
                diff.differences.append(
                    FieldDiff(f"ET_PO_HISTORY/{key}", name, old.get(name), new.get(name))
                )
    return diff


def route_vendor_lookup(
    request: VendorLookupRequest,
    vendor_data: Optional[dict],
    po_data: list[dict],
    *,
    legacy_handler: Optional[VendorLegacyHandler] = None,
    auth_check: Optional[AuthCheck] = None,
    flags_config: Optional[dict] = None,
    env: Optional[dict] = None,
) -> RoutedResult[VendorLookupResponse]:
    """Serve the lookup from the RFC or Python according to the vendor_lookup flag.

    Targeting: ``bukrs`` (company code) and ``lifnr`` (vendor number).
    SHADOW runs both paths, logs the diff and returns (or re-raises) the legacy outcome.
    """
    decision = resolve_flag(
        VENDOR_LOOKUP,
        {"bukrs": request.company_code, "lifnr": request.vendor_number},
        config=flags_config,
        env=env,
    )

    def python_path() -> VendorLookupResponse:
        if auth_check is not None and not auth_check(request.company_code):
            raise AuthorizationError(request.company_code)
        return lookup_vendor(request, vendor_data, po_data)

    if decision.mode == VendorLookupFlagMode.PYTHON:
        outcome, served_by, diff = _capture(python_path), "python", None
    else:
        if legacy_handler is None:
            raise LegacyHandlerRequired(VENDOR_LOOKUP, decision.mode)
        outcome, served_by, diff = _capture(lambda: legacy_handler(request)), "abap", None
        if decision.mode == VendorLookupFlagMode.SHADOW:
            try:
                diff = diff_vendor_outcomes(outcome, _capture(python_path))
            except Exception as exc:  # shadow path must never break the legacy path
                diff = ShadowDiff(python_error=repr(exc))

    routed = RoutedResult(
        outcome.response, decision, served_by, diff,
        details={"return_code": outcome.return_code},
    )
    emit_flag_event(routed)
    if outcome.exception is not None:
        raise outcome.exception
    return routed
