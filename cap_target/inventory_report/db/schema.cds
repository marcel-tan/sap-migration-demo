/**
 * SAP tables read by Z_INVENTORY_REPORT, modelled with their original
 * field names so the handler logic maps 1:1 onto the ABAP source.
 */
namespace acme.inventory;

/** MARA: general material data */
entity Materials {
  key matnr : String(40); // Material number
      mtart : String(4);  // Material type
      matkl : String(9);  // Material group
      lvorm : String(1);  // Deletion flag ('X' = flagged for deletion)
}

/** MAKT: material descriptions */
entity MaterialTexts {
  key matnr : String(40);
  key spras : String(2);  // Language key
      maktx : String(40); // Material description
}

/** MARC: plant data for material */
entity PlantData {
  key matnr : String(40);
  key werks : String(4);       // Plant
      minbe : Decimal(13, 3); // Reorder point
      mabst : Decimal(13, 3); // Maximum stock level
}

/** MARD: storage location data for material */
entity StorageData {
  key matnr : String(40);
  key werks : String(4);
  key lgort : String(4);       // Storage location
      labst : Decimal(13, 3); // Unrestricted stock
      insme : Decimal(13, 3); // Quality inspection stock
      speme : Decimal(13, 3); // Blocked stock
}

/** MKPF: material document header */
entity MaterialDocHeaders {
  key mblnr : String(10); // Material document number
  key mjahr : String(4);  // Material document year
      budat : Date;       // Posting date
}

/** MSEG: material document items */
entity MaterialDocItems {
  key mblnr  : String(10);
  key mjahr  : String(4);
  key zeile  : String(4);  // Line item
      matnr  : String(40);
      werks  : String(4);
      bwart  : String(3);  // Movement type ('101' = goods receipt)
      header : Association to MaterialDocHeaders
                 on header.mblnr = mblnr and header.mjahr = mjahr;
}
