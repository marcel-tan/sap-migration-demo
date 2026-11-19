using { acme.inventory as db } from '../db/schema';

/**
 * Migrated from ABAP report Z_INVENTORY_REPORT.
 * Selection screen -> generateReport parameters, ALV grid -> InventoryReport.
 */
service InventoryService {

  /** FORM fetch_inventory_data: MARA/MAKT/MARC/MARD join, deletion-flagged materials excluded */
  @readonly
  entity InventoryItems as
    select from db.Materials as m
      inner join db.MaterialTexts as t on t.matnr = m.matnr
      inner join db.PlantData     as c on c.matnr = m.matnr
      inner join db.StorageData   as d on  d.matnr = m.matnr
                                       and d.werks = c.werks
    {
      key m.matnr,
      key t.spras,
      key c.werks,
      key d.lgort,
          t.maktx,
          m.mtart,
          m.matkl,
          d.labst,
          d.insme,
          d.speme,
          c.minbe,
          c.mabst
    }
    where m.lvorm is null or m.lvorm = '';

  type StockStatus : String enum {
    critical; // ABAP '1' (red)
    warning;  // ABAP '2' (yellow)
    healthy;  // ABAP '3' (green)
  };

  /** ABAP ty_inventory */
  type InventoryReportItem {
    matnr        : String(40);
    maktx        : String(40);
    mtart        : String(4);
    matkl        : String(9);
    werks        : String(4);
    lgort        : String(4);
    labst        : Decimal(13, 3);
    insme        : Decimal(13, 3);
    speme        : Decimal(13, 3);
    total_stock  : Decimal(13, 3);
    minbe        : Decimal(13, 3);
    mabst        : Decimal(13, 3);
    stock_status : StockStatus;
    stock_pct    : Decimal(15, 2);
    last_receipt : Date;
    currency     : String(5);
    stock_value  : Decimal(15, 2);
  }

  /** Python InventoryReportSummary */
  type InventoryReportSummary {
    critical_count          : Integer;
    warning_count           : Integer;
    healthy_count           : Integer;
    total_stock_value       : Decimal(15, 2);
    materials_below_reorder : Integer;
    stale_materials         : Integer;
  }

  /** Selection screen blocks b01/b02 */
  type InventoryFilters {
    plants           : many String(4);
    storageLocations : many String(4);
    materialGroups   : many String(9);
    materialTypes    : many String(4);
    staleDays        : Integer;
    showCritical     : Boolean;
    showWarning      : Boolean;
    showHealthy      : Boolean;
  }

  type InventoryReport {
    generated_at    : Timestamp;
    filters_applied : InventoryFilters;
    total_items     : Integer;
    summary         : InventoryReportSummary;
    items           : many InventoryReportItem;
    message         : String; // ABAP MESSAGE ... TYPE 'S' when nothing is selected
  }

  action generateReport(
    plants           : many String(4),  // s_werks
    storageLocations : many String(4),  // s_lgort
    materialGroups   : many String(9),  // s_matkl
    materialTypes    : many String(4),  // s_mtart
    staleDays        : Integer default 90,     // p_stale
    showCritical     : Boolean default true,   // p_red
    showWarning      : Boolean default true,   // p_yellow
    showHealthy      : Boolean default false   // p_green
  ) returns InventoryReport;
}
