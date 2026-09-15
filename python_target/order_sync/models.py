"""
Data models for the Order Sync service.

Migrated from: Z_IDOC_ORDER_SYNC (IDoc Processing Function Module)
IDoc type:     ORDERS05
Segments:      E1EDK01, E1EDK03, E1EDKA1, E1EDP01, E1EDP19
"""

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class OrderStatus(str, Enum):
    """Processing result for each order.

    Maps to the IDoc status set by ABAP FORM set_idoc_status:
      '53' (msgty 'S') -> CREATED
      '51' (msgty 'E') -> FAILED
    """

    CREATED = "created"
    FAILED = "failed"


IDOC_STATUS_ERROR = "51"
IDOC_STATUS_SUCCESS = "53"
IDOC_STATUS_READY = "64"
IDOC_MESSAGE_TYPE_ORDERS = "ORDERS"
DEFAULT_DOC_TYPE = "ZOR"


class PartnerRole(str, Enum):
    """SAP partner function codes — maps to ABAP E1EDKA1-PARVW values."""

    SOLD_TO = "AG"    # Sold-to party
    SHIP_TO = "WE"    # Ship-to party
    BILL_TO = "RE"    # Bill-to party
    PAYER = "RG"      # Payer


class OrderPartner(BaseModel):
    """Partner in the order — maps to BAPIPARNR / E1EDKA1 segment."""

    role: PartnerRole = Field(description="Partner function (PARVW)")
    number: str = Field(description="Partner number (PARTN)")


class OrderItem(BaseModel):
    """Order line item — maps to E1EDP01 + E1EDP19 segments / BAPISDITM."""

    item_number: str = Field(default="", description="Item number (POSEX)")
    material_number: Optional[str] = Field(
        default=None, description="SAP material number from E1EDP19 qualifier 002"
    )
    customer_material: Optional[str] = Field(
        default=None, description="Customer material number from E1EDP19 qualifier 003"
    )
    plant: Optional[str] = Field(default=None, description="Delivering plant (WERKS)")
    quantity: Decimal = Field(default=Decimal("0"), description="Order quantity (MENGE)")
    unit_of_measure: str = Field(default="", description="Unit of measure (MENEE)")
    net_price: Decimal = Field(
        default=Decimal("0"), description="Net price (VPREI)"
    )
    item_category: Optional[str] = Field(
        default=None, description="Item category (PSTYV)"
    )


class OrderHeader(BaseModel):
    """Order header — maps to E1EDK01 + E1EDK03 + E1EDKA1 segments.

    Combines data from multiple IDoc segments into a single structure,
    replacing the ABAP ty_order_header type and its incremental population
    across FORM parse_header_segment / parse_date_segment / parse_partner_segment.
    """

    document_type: str = Field(
        default="", description="Sales document type (BSART/AUART); '' -> ZOR at BAPI mapping"
    )
    sales_org: str = Field(default="", description="Sales organization (VKORG)")
    distribution_channel: str = Field(default="", description="Distribution channel (VTWEG)")
    division: str = Field(default="", description="Division (SPART)")
    sold_to_party: str = Field(default="", description="Sold-to customer number (KUNAG)")
    ship_to_party: Optional[str] = Field(
        default=None, description="Ship-to customer number"
    )
    customer_po_number: Optional[str] = Field(
        default=None, description="Customer PO reference (BSTKD)"
    )
    customer_po_date: Optional[date] = Field(
        default=None, description="Customer PO date"
    )
    requested_delivery_date: Optional[date] = Field(
        default=None, description="Requested delivery date"
    )
    pricing_date: Optional[date] = Field(default=None, description="Pricing date")
    currency: str = Field(default="", description="Document currency (CURCY)")
    incoterms1: Optional[str] = Field(
        default=None, description="Incoterms part 1 (e.g., FOB)"
    )
    incoterms2: Optional[str] = Field(
        default=None, description="Incoterms part 2 (location)"
    )
    partners: list[OrderPartner] = Field(default_factory=list)
    items: list[OrderItem] = Field(default_factory=list)


class InboundOrderMessage(BaseModel):
    """Top-level inbound message — replaces the IDoc control + data structure.

    In the original ABAP, this was an IDoc (EDIDC control record + EDIDD data records).
    In the migrated system, this is a JSON message received from a message queue.
    """

    message_id: str = Field(description="Unique message ID (replaces IDoc DOCNUM)")
    message_type: str = Field(
        default=IDOC_MESSAGE_TYPE_ORDERS, description="Message type (replaces MESTYP)"
    )
    status: str = Field(
        default=IDOC_STATUS_READY,
        description="Inbound IDoc status (EDIDC-STATUS); only '64' is processed",
    )
    sender_system: Optional[str] = Field(
        default=None, description="Sending system identifier"
    )
    order: OrderHeader = Field(description="Parsed order data")


class BapiReturn(BaseModel):
    """Message returned by the order creation call — maps to BAPIRET2."""

    type: str = Field(description="Message type: S/I/W/E/A (BAPIRET2-TYPE)")
    message: str = Field(default="", description="Message text (BAPIRET2-MESSAGE)")


class OrderCreationResult(BaseModel):
    """Outcome of the target-system order creation call.

    Maps to BAPI_SALESORDER_CREATEFROMDAT2 IMPORTING salesdocument + TABLES return.
    """

    order_number: str = Field(default="", description="Sales document (VBELN)")
    messages: list[BapiReturn] = Field(default_factory=list)


class BapiSchedule(BaseModel):
    """Schedule line — maps to BAPISCHDL."""

    item_number: str
    schedule_line: str = "0001"
    requested_date: Optional[date] = None
    requested_quantity: Decimal


class BapiOrderPayload(BaseModel):
    """Payload sent to the target system — maps to the BAPI parameter set."""

    header: OrderHeader
    header_flags: dict[str, str]
    items: list[OrderItem]
    partners: list[OrderPartner]
    schedules: list[BapiSchedule]


class OrderSyncResult(BaseModel):
    """Processing result for a single order — replaces IDoc status record (BDIDOCSTAT)."""

    message_id: str
    status: OrderStatus
    idoc_status: str = Field(description="IDoc status code: 51=error, 53=success")
    message_type: str = Field(description="Message type: E or S (MSGTY)")
    message_v1: str = Field(default="", description="Message text, first 50 chars (MSGV1)")
    message_v2: str = Field(default="", description="Message text, chars 51-100 (MSGV2)")
    order_number: Optional[str] = Field(
        default=None, description="Created sales order number (VBELN)"
    )
    error_messages: list[str] = Field(default_factory=list)


class OrderSyncBatchResult(BaseModel):
    """Batch result — replaces the IDoc status table (BDIDOCSTAT)."""

    total_processed: int
    successful: int
    failed: int
    skipped: int = Field(
        default=0,
        description="Messages not matching MESTYP='ORDERS' AND STATUS='64'",
    )
    results: list[OrderSyncResult]
