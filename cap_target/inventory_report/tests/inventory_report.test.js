/**
 * Functional-equivalence tests for the CAP migration of Z_INVENTORY_REPORT.
 * Mirrors tests/test_inventory_report.py; each test references the ABAP logic it covers.
 */
const path = require('path')
const cds = require('@sap/cds')
const {
  StockStatus,
  calculateStockStatus,
  calculateStockPercentage,
  filterByStatus,
  buildSummary,
} = require('../srv/service')

const { GET, POST } = cds.test(path.join(__dirname, '..'))

const REPORT = '/odata/v4/inventory/generateReport'
const TODAY = new Date()
const daysAgo = n => {
  const d = new Date(TODAY)
  d.setDate(d.getDate() - n)
  return d
}
const isoDaysAgo = n => {
  const d = daysAgo(n)
  const pad = v => String(v).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

// ---------------------------------------------------------------------------
// FORM calculate_stock_status
// ---------------------------------------------------------------------------
describe('calculateStockStatus', () => {
  const status = (labst, minbe, lastReceipt = TODAY, staleDays = 90) =>
    calculateStockStatus(labst, minbe, lastReceipt, staleDays, TODAY)

  test('zero stock is critical (IF labst <= 0)', () => {
    expect(status(0, 100)).toBe(StockStatus.CRITICAL)
  })

  test('negative stock is critical (IF labst <= 0)', () => {
    expect(status(-5, 0)).toBe(StockStatus.CRITICAL)
  })

  test('below reorder point is critical (ELSEIF labst < minbe)', () => {
    expect(status(80, 100)).toBe(StockStatus.CRITICAL)
    expect(status(99.999, 100)).toBe(StockStatus.CRITICAL)
  })

  test('exactly at reorder point is warning (labst < minbe is false)', () => {
    expect(status(100, 100)).toBe(StockStatus.WARNING)
  })

  test('between reorder point and 1.5x is warning (ELSEIF labst < minbe * 1.5)', () => {
    expect(status(120, 100)).toBe(StockStatus.WARNING)
    expect(status(149.999, 100)).toBe(StockStatus.WARNING)
  })

  test('exactly 1.5x reorder point is healthy (ELSE)', () => {
    expect(status(150, 100)).toBe(StockStatus.HEALTHY)
  })

  test('above 1.5x reorder point is healthy (ELSE)', () => {
    expect(status(200, 100)).toBe(StockStatus.HEALTHY)
  })

  test('no reorder point defaults healthy', () => {
    expect(status(10, 0)).toBe(StockStatus.HEALTHY)
  })

  test('stale receipt downgrades healthy to warning (lv_age > p_stale AND status = 3)', () => {
    expect(status(200, 100, daysAgo(120))).toBe(StockStatus.WARNING)
  })

  test('receipt exactly staleDays old is not stale (lv_age > p_stale)', () => {
    expect(status(200, 100, daysAgo(90))).toBe(StockStatus.HEALTHY)
    expect(status(200, 100, daysAgo(91))).toBe(StockStatus.WARNING)
  })

  test('stale receipt does not change critical', () => {
    expect(status(50, 100, daysAgo(120))).toBe(StockStatus.CRITICAL)
  })

  test('stale receipt does not change warning', () => {
    expect(status(120, 100, daysAgo(120))).toBe(StockStatus.WARNING)
  })

  test('no receipt date skips the stale check (last_receipt IS NOT INITIAL)', () => {
    expect(status(200, 100, null)).toBe(StockStatus.HEALTHY)
  })

  test('custom staleDays is honoured (p_stale)', () => {
    expect(status(200, 100, daysAgo(31), 30)).toBe(StockStatus.WARNING)
    expect(status(200, 100, daysAgo(30), 30)).toBe(StockStatus.HEALTHY)
  })

  test('accepts ISO date strings for last_receipt', () => {
    expect(status(200, 100, isoDaysAgo(120))).toBe(StockStatus.WARNING)
  })
})

// ---------------------------------------------------------------------------
// FORM calculate_stock_status: stock percentage
// ---------------------------------------------------------------------------
describe('calculateStockPercentage', () => {
  test('normal percentage (labst / minbe) * 100', () => {
    expect(calculateStockPercentage(150, 100)).toBe(150)
  })

  test('zero reorder point returns 100', () => {
    expect(calculateStockPercentage(50, 0)).toBe(100)
  })

  test('below reorder point', () => {
    expect(calculateStockPercentage(30, 100)).toBe(30)
  })

  test('rounds to 2 decimals (stock_pct TYPE p DECIMALS 2)', () => {
    expect(calculateStockPercentage(1, 3)).toBe(33.33)
  })
})

// ---------------------------------------------------------------------------
// FORM filter_by_status
// ---------------------------------------------------------------------------
describe('filterByStatus', () => {
  const items = [StockStatus.CRITICAL, StockStatus.WARNING, StockStatus.HEALTHY].map(stock_status => ({
    matnr: 'MAT-001',
    werks: '1000',
    stock_status,
  }))

  test('show only critical (WHEN 1, p_red = X)', () => {
    const result = filterByStatus(items, { showCritical: true, showWarning: false, showHealthy: false })
    expect(result.map(i => i.stock_status)).toEqual([StockStatus.CRITICAL])
  })

  test('selection screen defaults show critical and warning', () => {
    const result = filterByStatus(items, { showCritical: true, showWarning: true, showHealthy: false })
    expect(result).toHaveLength(2)
  })

  test('show all', () => {
    expect(filterByStatus(items, { showCritical: true, showWarning: true, showHealthy: true })).toHaveLength(3)
  })

  test('show none', () => {
    expect(filterByStatus(items, { showCritical: false, showWarning: false, showHealthy: false })).toHaveLength(0)
  })
})

describe('buildSummary', () => {
  test('counts per status, total value, below reorder and stale', () => {
    const summary = buildSummary([
      { stock_status: 'critical', stock_value: 100, labst: 10, minbe: 50, stale: false },
      { stock_status: 'warning', stock_value: 200.5, labst: 60, minbe: 50, stale: true },
      { stock_status: 'healthy', stock_value: 300, labst: 100, minbe: 0, stale: false },
    ])
    expect(summary).toEqual({
      critical_count: 1,
      warning_count: 1,
      healthy_count: 1,
      total_stock_value: 600.5,
      materials_below_reorder: 1,
      stale_materials: 1,
    })
  })
})

// ---------------------------------------------------------------------------
// Bundled CSV sample data (service runs standalone)
// ---------------------------------------------------------------------------
describe('InventoryItems view (sample data)', () => {
  test('joins MARA/MAKT/MARC/MARD and excludes deletion-flagged materials', async () => {
    const { data } = await GET("/odata/v4/inventory/InventoryItems?$filter=spras eq 'EN'")
    const matnrs = data.value.map(r => r.matnr)
    expect(data.value).toHaveLength(7)
    expect(matnrs).not.toContain('MAT-1006') // lvorm = 'X'
    expect(data.value.find(r => r.matnr === 'MAT-1001' && r.werks === '1000')).toMatchObject({
      maktx: 'Widget A',
      lgort: '0001',
    })
  })

  test('generateReport runs against the sample data with default selection', async () => {
    const { data } = await POST(REPORT, {})
    expect(data.total_items).toBeGreaterThan(0)
    expect(data.items.every(i => i.stock_status !== StockStatus.HEALTHY)).toBe(true)
    expect(data.items.map(i => i.matnr)).not.toContain('MAT-1006')
  })
})

// ---------------------------------------------------------------------------
// generateReport end to end (START-OF-SELECTION) with deterministic fixtures
// ---------------------------------------------------------------------------
describe('generateReport', () => {
  let db
  let T

  // Each material gets its own MKPF document so receipt dates are relative to today.
  async function seed(materials) {
    for (const entity of Object.values(T)) await db.run(DELETE.from(entity))
    let doc = 4900000000
    const seen = new Set()
    for (const m of materials) {
      if (!seen.has(m.matnr)) {
        seen.add(m.matnr)
        await db.run(INSERT.into(T.Materials).entries({ matnr: m.matnr, mtart: m.mtart ?? 'FERT', matkl: m.matkl ?? '001', lvorm: m.lvorm ?? '' }))
        await db.run(INSERT.into(T.MaterialTexts).entries({ matnr: m.matnr, spras: 'EN', maktx: m.maktx ?? m.matnr }))
      }
      await db.run(INSERT.into(T.PlantData).entries({ matnr: m.matnr, werks: m.werks, minbe: m.minbe ?? 0, mabst: m.mabst ?? 0 }))
      await db.run(INSERT.into(T.StorageData).entries({
        matnr: m.matnr, werks: m.werks, lgort: m.lgort ?? '0001',
        labst: m.labst ?? 0, insme: m.insme ?? 0, speme: m.speme ?? 0,
      }))
      for (const r of m.receipts ?? []) {
        const mblnr = String(++doc)
        await db.run(INSERT.into(T.MaterialDocHeaders).entries({ mblnr, mjahr: '2026', budat: isoDaysAgo(r.daysAgo) }))
        await db.run(INSERT.into(T.MaterialDocItems).entries({
          mblnr, mjahr: '2026', zeile: '0001', matnr: m.matnr, werks: m.werks, bwart: r.bwart ?? '101',
        }))
      }
    }
  }

  const ALL = { showCritical: true, showWarning: true, showHealthy: true }

  beforeAll(async () => {
    db = await cds.connect.to('db')
    const e = cds.entities('acme.inventory')
    T = {
      Materials: e.Materials,
      MaterialTexts: e.MaterialTexts,
      PlantData: e.PlantData,
      StorageData: e.StorageData,
      MaterialDocHeaders: e.MaterialDocHeaders,
      MaterialDocItems: e.MaterialDocItems,
    }
  })

  test('full report generation, sorted by status then plant, with summary', async () => {
    await seed([
      { matnr: 'MAT-001', maktx: 'Widget A', werks: '1000', labst: 200, insme: 10, speme: 5, minbe: 100, mabst: 500, receipts: [{ daysAgo: 0 }] },
      { matnr: 'MAT-002', maktx: 'Widget B', werks: '1000', labst: 30, minbe: 100, receipts: [{ daysAgo: 0 }] },
      { matnr: 'MAT-003', maktx: 'Widget C', werks: '2000', matkl: '002', labst: 0, minbe: 50 },
      { matnr: 'MAT-004', maktx: 'Widget D', werks: '0500', labst: 120, minbe: 100 },
    ])

    const { status, data } = await POST(REPORT, ALL)
    expect(status).toBe(200)
    expect(data.total_items).toBe(4)
    expect(data.message).toBeNull()
    expect(data.items.map(i => [i.stock_status, i.werks, i.matnr])).toEqual([
      ['critical', '1000', 'MAT-002'],
      ['critical', '2000', 'MAT-003'],
      ['warning', '0500', 'MAT-004'],
      ['healthy', '1000', 'MAT-001'],
    ])

    const widgetA = data.items.find(i => i.matnr === 'MAT-001')
    expect(widgetA).toMatchObject({
      maktx: 'Widget A',
      total_stock: 215,      // labst + insme + speme
      stock_value: 2150,     // total_stock * 10
      stock_pct: 200,
      currency: 'USD',
      last_receipt: isoDaysAgo(0),
    })

    expect(data.summary).toEqual({
      critical_count: 2,
      warning_count: 1,
      healthy_count: 1,
      total_stock_value: 2150 + 300 + 0 + 1200,
      materials_below_reorder: 2,
      stale_materials: 0,
    })
  })

  test('default status filters exclude healthy items (p_green = space)', async () => {
    await seed([{ matnr: 'MAT-001', werks: '1000', labst: 500, minbe: 100, receipts: [{ daysAgo: 0 }] }])
    const { data } = await POST(REPORT, {})
    expect(data.total_items).toBe(0)
    expect(data.message).toBe('No materials match the selected status filters.')
    expect(data.filters_applied).toMatchObject({ staleDays: 90, showCritical: true, showWarning: true, showHealthy: false })
  })

  test('status checkboxes filter the result (FORM filter_by_status)', async () => {
    await seed([
      { matnr: 'MAT-RED', werks: '1000', labst: 0, minbe: 100 },
      { matnr: 'MAT-YEL', werks: '1000', labst: 120, minbe: 100 },
      { matnr: 'MAT-GRN', werks: '1000', labst: 500, minbe: 100 },
    ])
    const only = async flags => (await POST(REPORT, flags)).data.items.map(i => i.matnr)
    expect(await only({ showCritical: true, showWarning: false, showHealthy: false })).toEqual(['MAT-RED'])
    expect(await only({ showCritical: false, showWarning: true, showHealthy: false })).toEqual(['MAT-YEL'])
    expect(await only({ showCritical: false, showWarning: false, showHealthy: true })).toEqual(['MAT-GRN'])
    expect(await only({})).toEqual(['MAT-RED', 'MAT-YEL'])
  })

  test('last receipt uses MAX(budat) of bwart 101 only, per matnr/werks', async () => {
    await seed([
      {
        matnr: 'MAT-001', werks: '1000', labst: 500, minbe: 100,
        receipts: [{ daysAgo: 200 }, { daysAgo: 120 }, { daysAgo: 1, bwart: '261' }],
      },
      { matnr: 'MAT-001', werks: '2000', labst: 500, minbe: 100, receipts: [{ daysAgo: 5 }] },
    ])
    const { data } = await POST(REPORT, ALL)
    const byPlant = Object.fromEntries(data.items.map(i => [i.werks, i]))
    expect(byPlant['1000'].last_receipt).toBe(isoDaysAgo(120))
    expect(byPlant['1000'].stock_status).toBe(StockStatus.WARNING) // stale downgrade
    expect(byPlant['2000'].last_receipt).toBe(isoDaysAgo(5))
    expect(byPlant['2000'].stock_status).toBe(StockStatus.HEALTHY)
    expect(data.summary.stale_materials).toBe(1)
  })

  test('staleDays parameter drives the stale downgrade (p_stale)', async () => {
    await seed([{ matnr: 'MAT-001', werks: '1000', labst: 500, minbe: 100, receipts: [{ daysAgo: 45 }] }])
    expect((await POST(REPORT, ALL)).data.items[0].stock_status).toBe(StockStatus.HEALTHY)
    expect((await POST(REPORT, { ...ALL, staleDays: 30 })).data.items[0].stock_status).toBe(StockStatus.WARNING)
  })

  test('invalid staleDays is rejected', async () => {
    await expect(POST(REPORT, { staleDays: 0 })).rejects.toMatchObject({ response: { status: 400 } })
  })

  describe('select-options (s_werks, s_lgort, s_matkl, s_mtart)', () => {
    beforeEach(() =>
      seed([
        { matnr: 'MAT-A', werks: '1000', lgort: '0001', matkl: '001', mtart: 'FERT' },
        { matnr: 'MAT-B', werks: '2000', lgort: '0002', matkl: '002', mtart: 'ROH' },
        { matnr: 'MAT-C', werks: '3000', lgort: '0001', matkl: '002', mtart: 'HALB' },
        { matnr: 'MAT-D', werks: '1000', lgort: '0001', matkl: '001', mtart: 'FERT', lvorm: 'X' },
      ])
    )
    const matnrs = async params => (await POST(REPORT, { ...ALL, ...params })).data.items.map(i => i.matnr).sort()

    test('empty select-options return everything except deletion-flagged (lvorm = space)', async () => {
      expect(await matnrs({})).toEqual(['MAT-A', 'MAT-B', 'MAT-C'])
    })
    test('plants', async () => {
      expect(await matnrs({ plants: ['1000', '3000'] })).toEqual(['MAT-A', 'MAT-C'])
    })
    test('storageLocations', async () => {
      expect(await matnrs({ storageLocations: ['0002'] })).toEqual(['MAT-B'])
    })
    test('materialGroups', async () => {
      expect(await matnrs({ materialGroups: ['002'] })).toEqual(['MAT-B', 'MAT-C'])
    })
    test('materialTypes', async () => {
      expect(await matnrs({ materialTypes: ['FERT', 'HALB'] })).toEqual(['MAT-A', 'MAT-C'])
    })
    test('combined select-options are ANDed', async () => {
      expect(await matnrs({ materialGroups: ['002'], storageLocations: ['0001'] })).toEqual(['MAT-C'])
    })
    test('no match returns an empty report (IF sy-subrc <> 0)', async () => {
      const { data } = await POST(REPORT, { plants: ['9999'] })
      expect(data.total_items).toBe(0)
      expect(data.items).toEqual([])
      expect(data.message).toBe('No materials found for the given selection criteria.')
    })
  })
})
