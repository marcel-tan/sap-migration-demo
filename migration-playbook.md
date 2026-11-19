# SAP ABAP → Python Migration Playbook

> **Purpose**: Reusable playbook for Devin to migrate custom ABAP objects to Python services.
> Designed for batch execution across hundreds of custom objects.

---

## Scope

This playbook covers migration of **code-based** SAP customizations:

| Source (SAP)              | Target (Python)                     | Pattern     |
|---------------------------|-------------------------------------|-------------|
| Custom ALV reports (Z*)   | FastAPI endpoint + pandas           | Report      |
| RFC function modules      | REST API endpoint                   | Interface   |
| IDoc processing functions | Event-driven service (queue-based)  | Interface   |
| Custom BAdI implementations | Service layer module              | Enhancement |
| SAPscript / Smart Forms   | PDF generation (WeasyPrint/ReportLab) | Report    |

**Out of scope** (requires SAP functional consultants):
- IMG configuration / customizing
- Pricing procedures
- Output determination
- Workflow rules
- Authorization role design

---

## Pre-Migration Checklist

Before starting migration of any ABAP object:

- [ ] Source ABAP code is exported and accessible
- [ ] Business owner has confirmed the object is still in use
- [ ] Target architecture is defined (API framework, database, deployment)
- [ ] Data dictionary dependencies are mapped (which SAP tables are read/written)
- [ ] Test data is available (sample inputs and expected outputs)
- [ ] Acceptance criteria defined (functional equivalence tests)

---

## Migration Steps

### Step 1: Analyze the ABAP Source

Read the ABAP program and identify:

1. **Input parameters**: Selection screen fields, function module imports, IDoc segments
2. **Data sources**: SAP tables accessed (MARA, EKKO, VBAK, etc.) and their joins
3. **Business logic**: Calculations, validations, status determinations, conditional flows
4. **Output format**: ALV grid, RFC exports, IDoc status, file output
5. **Dependencies**: Called function modules, BAPIs, includes, macros
6. **Error handling**: SY-SUBRC checks, exception handling, message types

Document these in a structured format before writing any Python code.

### Step 2: Design the Python Target

Map each ABAP component to its Python equivalent:

| ABAP Concept            | Python Equivalent                    |
|-------------------------|--------------------------------------|
| Selection screen        | API query parameters / request body  |
| TYPE definition         | Pydantic BaseModel                   |
| INTERNAL TABLE          | `list[Model]`                        |
| FIELD-SYMBOL            | Loop variable / list comprehension   |
| SELECT ... INTO TABLE   | SQL query → DataFrame or ORM query   |
| AUTHORITY-CHECK         | JWT / API key middleware              |
| SY-SUBRC check          | Exception handling / if-else         |
| MESSAGE TYPE 'E'        | HTTPException / raise                |
| ALV display             | JSON response                        |
| RFC interface           | REST/GraphQL endpoint                |
| IDoc processing         | Message queue consumer               |
| BAPI call               | Target system API call               |
| COMMIT WORK             | Database transaction commit          |
| ROLLBACK WORK           | Database transaction rollback        |

### Step 3: Create Data Models

For each ABAP TYPE definition or structure:

```python
from pydantic import BaseModel, Field

class MaterialStock(BaseModel):
    """Maps to ABAP ty_inventory structure.
    
    Source fields: MARA-MATNR, MAKT-MAKTX, MARD-LABST, etc.
    """
    material_number: str = Field(description="MATNR")
    description: str = Field(description="MAKTX")
    available_stock: Decimal = Field(description="LABST")
```

**Rules**:
- One model per ABAP structure/table type
- Include Field descriptions mapping to SAP field names
- Use Python-native types (Decimal for quantities/amounts, date for DATS)
- Add docstrings referencing the source ABAP type name

### Step 4: Implement Business Logic

Translate ABAP FORMs/methods to Python functions:

- Each ABAP `FORM` or method → one Python function
- Preserve the same function name (converted to snake_case)
- Add comments mapping to ABAP logic for traceability
- Keep the same control flow structure where possible

**Critical**: Do NOT "improve" the business logic during migration.
The goal is **functional equivalence**, not optimization.
Optimizations come in a separate phase after migration is validated.

### Step 5: Write Equivalence Tests

For each migrated function, write tests that verify:

1. **Happy path**: Same input → same output as ABAP
2. **Edge cases**: Empty tables, zero values, null/initial fields
3. **Boundary conditions**: Threshold values (e.g., stock = reorder point exactly)
4. **Error cases**: Invalid input → same error behavior as ABAP

Test naming convention:
```python
def test_<function_name>_<scenario>():
    """ABAP: <reference to original logic>."""
```

### Step 6: Validate and Review

- [ ] All tests pass
- [ ] Every ABAP FORM/method has a corresponding Python function
- [ ] Every ABAP TYPE has a corresponding Pydantic model
- [ ] Business logic matches 1:1 (no accidental "improvements")
- [ ] Error handling covers all ABAP SY-SUBRC / MESSAGE paths
- [ ] Comments reference original ABAP code for traceability

### Step 7: Roll Out Behind a Feature Flag

- [ ] Add the object to `python_target/feature_flags.py` with legacy as the default mode
- [ ] Add a router that takes an injectable `legacy_handler` and emits `emit_flag_event()`
- [ ] Run `shadow` (or `validate_only` for inbound IDocs) until `diff.clean` holds for the agreed window
- [ ] Move a canary cohort (`bukrs`, `werks`, `lifnr`, `docnum`, `sndprn`) to `python`, then go global
- [ ] Remove the flag, router and legacy handler once ABAP is decommissioned

See the "Feature-flag rollout" section in `README.md`.

---

## Migration Patterns by Object Type

### Pattern A: ALV Report → FastAPI + JSON

```
ABAP Flow:                      Python Flow:
─────────────                   ────────────
Selection Screen  ─────────►    Query Parameters / Request Body
    │                               │
    ▼                               ▼
SELECT from DB    ─────────►    SQL Query / ORM / Data Warehouse
    │                               │
    ▼                               ▼
LOOP + Calculate  ─────────►    List comprehension + functions
    │                               │
    ▼                               ▼
Filter + Sort     ─────────►    filter() + sort()
    │                               │
    ▼                               ▼
ALV Display       ─────────►    JSON Response + Dashboard
```

### Pattern B: RFC Function → REST API

```
ABAP Flow:                      Python Flow:
─────────────                   ────────────
IMPORTING params  ─────────►    Path/query params or request body
    │                               │
    ▼                               ▼
AUTHORITY-CHECK   ─────────►    Auth middleware (JWT/API key)
    │                               │
    ▼                               ▼
SELECT data       ─────────►    Database query
    │                               │
    ▼                               ▼
Process + calc    ─────────►    Service function
    │                               │
    ▼                               ▼
EXPORTING params  ─────────►    JSON response body
    │                               │
    ▼                               ▼
RAISE exception   ─────────►    HTTPException / error response
```

### Pattern C: IDoc Processing → Event-Driven Service

```
ABAP Flow:                      Python Flow:
─────────────                   ────────────
IDoc Control rec  ─────────►    Message envelope (ID, type, sender)
    │                               │
    ▼                               ▼
Parse segments    ─────────►    JSON deserialization
(E1EDK01, etc.)                 (Pydantic model validation)
    │                               │
    ▼                               ▼
Validate data     ─────────►    validate_order() function
    │                               │
    ▼                               ▼
Map to BAPI       ─────────►    Map to target system payload
    │                               │
    ▼                               ▼
BAPI call         ─────────►    Target API call
    │                               │
    ▼                               ▼
COMMIT/ROLLBACK   ─────────►    Transaction commit/rollback
    │                               │
    ▼                               ▼
IDoc status       ─────────►    Processing result record
```

---

## Common ABAP → Python Translations

### Internal Table Operations

```abap
" ABAP
LOOP AT lt_data INTO ls_data WHERE status = 'A'.
  ls_data-amount = ls_data-quantity * ls_data-price.
  MODIFY lt_data FROM ls_data.
ENDLOOP.
```

```python
# Python
for item in data:
    if item.status == "A":
        item.amount = item.quantity * item.price
```

### SELECT with JOIN

```abap
" ABAP
SELECT a~matnr b~maktx a~mtart
  INTO TABLE lt_materials
  FROM mara AS a
  INNER JOIN makt AS b ON b~matnr = a~matnr AND b~spras = sy-langu
  WHERE a~mtart IN s_mtart.
```

```python
# Python (SQLAlchemy)
query = (
    select(Material.number, MaterialText.description, Material.type)
    .join(MaterialText, MaterialText.material == Material.number)
    .where(MaterialText.language == locale)
    .where(Material.type.in_(material_types))
)
materials = session.execute(query).all()
```

### Error Handling

```abap
" ABAP
CALL FUNCTION 'BAPI_SALESORDER_CREATEFROMDAT2'
  EXPORTING ...
  IMPORTING salesdocument = lv_vbeln
  TABLES return = lt_return.

LOOP AT lt_return INTO ls_ret WHERE type CA 'EA'.
  lv_has_error = abap_true.
ENDLOOP.

IF lv_has_error = abap_true.
  CALL FUNCTION 'BAPI_TRANSACTION_ROLLBACK'.
ELSE.
  CALL FUNCTION 'BAPI_TRANSACTION_COMMIT' EXPORTING wait = 'X'.
ENDIF.
```

```python
# Python
try:
    order_number = create_order(order_data)
    db.commit()
    return OrderResult(status="created", order_number=order_number)
except OrderCreationError as exc:
    db.rollback()
    return OrderResult(status="failed", errors=[str(exc)])
```

---

## Quality Checklist

Before marking any migrated object as complete:

- [ ] All unit tests pass
- [ ] Code follows project style guide (PEP 8, type hints, docstrings)
- [ ] No ABAP-isms in Python (e.g., SY-SUBRC patterns, INITIAL checks)
- [ ] Pydantic models validate input/output correctly
- [ ] Error messages are clear and actionable
- [ ] Logging captures key business events
- [ ] Migration comments reference original ABAP line/form names
- [ ] No hardcoded values that should be configurable
- [ ] Performance is acceptable for expected data volumes
