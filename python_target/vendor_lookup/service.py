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

from datetime import date, timedelta
from decimal import Decimal
from typing import Callable, Optional

from .models import (
    PurchaseOrderItem,
    VendorDetail,
    VendorLookupRequest,
    VendorLookupResponse,
)


# ABAP: EV_RETURN_CODE  0=OK, 1=not found, 2=error
RETURN_CODE_OK = 0
RETURN_CODE_NOT_FOUND = 1
RETURN_CODE_ERROR = 2

# ABAP: AUTHORITY-CHECK OBJECT 'F_LFA1_BUK' ID 'BUKRS' FIELD iv_bukrs ID 'ACTVT' FIELD '03'.
AUTH_OBJECT = "F_LFA1_BUK"
AUTH_ACTIVITY_DISPLAY = "03"

AuthorizationChecker = Callable[[str, str], bool]


class VendorNotFoundError(Exception):
    """Replaces ABAP RAISE vendor_not_found (ev_return_code = 1)."""

    return_code = RETURN_CODE_NOT_FOUND

    def __init__(self, vendor_number: str) -> None:
        self.vendor_number = vendor_number
        super().__init__(f"Vendor {vendor_number} not found")


class AuthorizationError(Exception):
    """Replaces ABAP RAISE authorization_failed (ev_return_code = 2)."""

    return_code = RETURN_CODE_ERROR

    def __init__(self, company_code: str) -> None:
        self.company_code = company_code
        super().__init__(f"Authorization failed for company code {company_code}")


def _allow_all(company_code: str, activity: str) -> bool:
    return True


def check_authorization(
    company_code: str,
    is_authorized: AuthorizationChecker,
) -> None:
    """AUTHORITY-CHECK OBJECT 'F_LFA1_BUK' ID 'BUKRS' FIELD iv_bukrs ID 'ACTVT' FIELD '03'.

    ABAP: IF sy-subrc <> 0. ev_return_code = 2. RAISE authorization_failed.
    """
    if not is_authorized(company_code, AUTH_ACTIVITY_DISPLAY):
        raise AuthorizationError(company_code)


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
    is_authorized: AuthorizationChecker = _allow_all,
) -> VendorLookupResponse:
    """Main lookup logic — replaces the ABAP function module body.

    Args:
        request:       Lookup parameters (vendor number, company code, etc.)
        vendor_data:   Vendor master record from data warehouse.
                       None if vendor not found.
        po_data:       Purchase order line items from data warehouse.
        is_authorized: (company_code, activity) -> bool; replaces AUTHORITY-CHECK.

    Returns:
        VendorLookupResponse with vendor details and PO history.

    Raises:
        AuthorizationError:  If the caller lacks F_LFA1_BUK display for company_code.
        VendorNotFoundError: If vendor_data is None.
    """
    # Authorization check runs before any data access — ABAP: RAISE authorization_failed.
    check_authorization(request.company_code, is_authorized)

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
        if po_date is None or po_date < date_from:
            continue

        # ABAP: AND h~loekz = ' ' AND p~loekz = ' ' (header/item not deleted)
        if row.get("header_deleted", False) or row.get("item_deleted", False):
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
        return_code=RETURN_CODE_OK,
        return_message=(
            f"Vendor {request.vendor_number} retrieved successfully. "
            f"{len(po_items)} PO items returned."
        ),
    )
