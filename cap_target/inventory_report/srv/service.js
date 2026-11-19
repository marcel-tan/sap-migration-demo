/**
 * InventoryService: migrated from ABAP report Z_INVENTORY_REPORT.
 *
 * START-OF-SELECTION                 -> generateReport handler
 *   PERFORM fetch_inventory_data     -> fetchInventoryData()
 *   PERFORM calculate_stock_status   -> calculateStockStatus() / calculateStockPercentage()
 *   PERFORM filter_by_status         -> filterByStatus()
 *   PERFORM display_alv              -> displayAlv() (sorted JSON + summary instead of ALV grid)
 */
const cds = require('@sap/cds')

const SY_LANGU = 'EN'                 // sy-langu
const BWART_GOODS_RECEIPT = '101'     // MSEG-BWART goods receipt
const PLACEHOLDER_UNIT_COST = 10      // ABAP: total_stock * 10 (MBEW in production)
const CURRENCY = 'USD'
const MS_PER_DAY = 24 * 60 * 60 * 1000

const StockStatus = Object.freeze({
  CRITICAL: 'critical', // ABAP stock_status '1' (red)
  WARNING: 'warning',   // ABAP stock_status '2' (yellow)
  HEALTHY: 'healthy',   // ABAP stock_status '3' (green)
})

// ALV sort: STOCK_STATUS ascending ('1' < '2' < '3')
const STATUS_SORT_ORDER = { critical: 1, warning: 2, healthy: 3 }

// Selection screen defaults: p_stale DEFAULT 90, p_red/p_yellow DEFAULT 'X', p_green DEFAULT ' '
const DEFAULT_FILTERS = Object.freeze({
  plants: [],
  storageLocations: [],
  materialGroups: [],
  materialTypes: [],
  staleDays: 90,
  showCritical: true,
  showWarning: true,
  showHealthy: false,
})

const round2 = n => Math.round((n + Number.EPSILON) * 100) / 100

// sy-datum: local calendar date as YYYY-MM-DD
function toAbapDate(value = new Date()) {
  if (typeof value === 'string') return value.slice(0, 10)
  const pad = n => String(n).padStart(2, '0')
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}`
}

// ABAP date arithmetic: lv_age = lv_today - last_receipt (days)
function daysBetween(from, to) {
  const utc = d => Date.parse(`${toAbapDate(d)}T00:00:00Z`)
  return Math.round((utc(to) - utc(from)) / MS_PER_DAY)
}

function isStale(lastReceipt, staleDays, today = new Date()) {
  // IF <fs_inv>-last_receipt IS NOT INITIAL. ... IF lv_age > p_stale
  return Boolean(lastReceipt) && daysBetween(lastReceipt, today) > staleDays
}

/** FORM calculate_stock_status: traffic-light thresholds */
function calculateStockStatus(labst, minbe, lastReceipt, staleDays, today = new Date()) {
  labst = Number(labst ?? 0)
  minbe = Number(minbe ?? 0)

  let status
  if (labst <= 0) status = StockStatus.CRITICAL             // IF labst <= 0 -> '1'
  else if (labst < minbe) status = StockStatus.CRITICAL     // ELSEIF labst < minbe -> '1'
  else if (labst < minbe * 1.5) status = StockStatus.WARNING // ELSEIF labst < minbe * '1.5' -> '2'
  else status = StockStatus.HEALTHY                         // ELSE -> '3'

  // Stale override: IF lv_age > p_stale AND stock_status = '3' -> '2'
  if (status === StockStatus.HEALTHY && isStale(lastReceipt, staleDays, today)) {
    status = StockStatus.WARNING
  }
  return status
}

/** FORM calculate_stock_status: IF minbe > 0 -> (labst / minbe) * 100 ELSE 100 */
function calculateStockPercentage(labst, minbe) {
  labst = Number(labst ?? 0)
  minbe = Number(minbe ?? 0)
  return minbe > 0 ? round2((labst / minbe) * 100) : 100 // stock_pct TYPE p DECIMALS 2
}

/** One LOOP AT gt_inventory pass of FORM fetch_inventory_data + FORM calculate_stock_status */
function buildItem(row, staleDays, today = new Date()) {
  const labst = Number(row.labst ?? 0)
  const insme = Number(row.insme ?? 0)
  const speme = Number(row.speme ?? 0)
  const minbe = Number(row.minbe ?? 0)
  const total_stock = labst + insme + speme // total_stock = labst + insme + speme
  const last_receipt = row.last_receipt ?? null

  return {
    matnr: row.matnr,
    maktx: row.maktx ?? '',
    mtart: row.mtart ?? '',
    matkl: row.matkl ?? '',
    werks: row.werks,
    lgort: row.lgort ?? '',
    labst,
    insme,
    speme,
    total_stock,
    minbe,
    mabst: Number(row.mabst ?? 0),
    stock_status: calculateStockStatus(labst, minbe, last_receipt, staleDays, today),
    stock_pct: calculateStockPercentage(labst, minbe),
    last_receipt,
    currency: CURRENCY,                                         // currency = 'USD'
    stock_value: round2(total_stock * PLACEHOLDER_UNIT_COST),   // stock_value = total_stock * 10
    stale: isStale(last_receipt, staleDays, today),
  }
}

/** FORM filter_by_status: CASE stock_status WHEN '1'/'2'/'3' + p_red/p_yellow/p_green */
function filterByStatus(items, filters) {
  const allowed = new Set()
  if (filters.showCritical) allowed.add(StockStatus.CRITICAL)
  if (filters.showWarning) allowed.add(StockStatus.WARNING)
  if (filters.showHealthy) allowed.add(StockStatus.HEALTHY)
  return items.filter(item => allowed.has(item.stock_status))
}

/** Python build_summary(): aggregate the ALV rows (no ABAP equivalent) */
function buildSummary(items) {
  const summary = {
    critical_count: 0,
    warning_count: 0,
    healthy_count: 0,
    total_stock_value: 0,
    materials_below_reorder: 0,
    stale_materials: 0,
  }
  for (const item of items) {
    if (item.stock_status === StockStatus.CRITICAL) summary.critical_count++
    else if (item.stock_status === StockStatus.WARNING) summary.warning_count++
    else summary.healthy_count++

    summary.total_stock_value += item.stock_value
    if (item.minbe > 0 && item.labst < item.minbe) summary.materials_below_reorder++
    if (item.stale) summary.stale_materials++
  }
  summary.total_stock_value = round2(summary.total_stock_value)
  return summary
}

/** FORM display_alv: sort STOCK_STATUS up (position 1), WERKS up (position 2) */
function displayAlv(items, filters, message = null) {
  const sorted = [...items].sort(
    (a, b) =>
      STATUS_SORT_ORDER[a.stock_status] - STATUS_SORT_ORDER[b.stock_status] ||
      String(a.werks).localeCompare(String(b.werks))
  )
  return {
    generated_at: new Date().toISOString(),
    filters_applied: filters,
    total_items: sorted.length,
    summary: buildSummary(sorted),
    items: sorted.map(({ stale, ...item }) => item),
    message,
  }
}

function normalizeFilters(data = {}) {
  const list = v => (Array.isArray(v) ? v.filter(x => x != null && x !== '') : [])
  return {
    plants: list(data.plants),
    storageLocations: list(data.storageLocations),
    materialGroups: list(data.materialGroups),
    materialTypes: list(data.materialTypes),
    staleDays: data.staleDays ?? DEFAULT_FILTERS.staleDays,
    showCritical: data.showCritical ?? DEFAULT_FILTERS.showCritical,
    showWarning: data.showWarning ?? DEFAULT_FILTERS.showWarning,
    showHealthy: data.showHealthy ?? DEFAULT_FILTERS.showHealthy,
  }
}

class InventoryService extends cds.ApplicationService {
  init() {
    this.on('generateReport', req => this.onGenerateReport(req))
    return super.init()
  }

  /** START-OF-SELECTION */
  async onGenerateReport(req) {
    const filters = normalizeFilters(req.data)
    if (!Number.isInteger(filters.staleDays) || filters.staleDays < 1) {
      return req.reject(400, 'staleDays must be a positive integer')
    }
    const today = new Date() // lv_today = sy-datum

    const rows = await this.fetchInventoryData(filters)
    if (!rows.length) {
      return displayAlv([], filters, 'No materials found for the given selection criteria.')
    }

    const items = rows.map(row => buildItem(row, filters.staleDays, today))

    const filtered = filterByStatus(items, filters)
    if (!filtered.length) {
      return displayAlv([], filters, 'No materials match the selected status filters.')
    }

    return displayAlv(filtered, filters)
  }

  /** FORM fetch_inventory_data */
  async fetchInventoryData(filters) {
    const { InventoryItems } = this.entities
    const { MaterialDocItems } = cds.entities('acme.inventory')

    // SELECT ... FROM mara JOIN makt (spras = sy-langu) JOIN marc JOIN mard
    // WHERE werks IN s_werks AND lgort IN s_lgort AND matkl IN s_matkl AND mtart IN s_mtart
    //   AND lvorm = ' '   (lvorm is applied by the InventoryItems view)
    // An empty select-option means "all", as in ABAP.
    const where = { spras: SY_LANGU }
    if (filters.plants.length) where.werks = { in: filters.plants }
    if (filters.storageLocations.length) where.lgort = { in: filters.storageLocations }
    if (filters.materialGroups.length) where.matkl = { in: filters.materialGroups }
    if (filters.materialTypes.length) where.mtart = { in: filters.materialTypes }
    const rows = await SELECT.from(InventoryItems).where(where)

    // IF sy-subrc <> 0 -> LEAVE LIST-PROCESSING
    if (!rows.length) return rows

    // lt_matnr: SORT + DELETE ADJACENT DUPLICATES
    const matnrs = [...new Set(rows.map(r => r.matnr))].sort()

    // SELECT g~matnr g~werks MAX( k~budat ) FROM mseg AS g JOIN mkpf AS k
    //   WHERE g~matnr IN lt_matnr AND g~bwart = '101' GROUP BY g~matnr g~werks
    const receipts = await SELECT.from(MaterialDocItems)
      .columns('matnr', 'werks', 'max(header.budat) as budat')
      .where({ matnr: { in: matnrs }, bwart: BWART_GOODS_RECEIPT })
      .groupBy('matnr', 'werks')

    // READ TABLE lt_receipts WITH KEY matnr werks -> last_receipt
    const lastReceipt = new Map(receipts.map(r => [`${r.matnr}|${r.werks}`, r.budat]))
    return rows.map(row => ({ ...row, last_receipt: lastReceipt.get(`${row.matnr}|${row.werks}`) ?? null }))
  }
}

module.exports = InventoryService
Object.assign(module.exports, {
  StockStatus,
  DEFAULT_FILTERS,
  calculateStockStatus,
  calculateStockPercentage,
  buildItem,
  filterByStatus,
  buildSummary,
  displayAlv,
  normalizeFilters,
})
