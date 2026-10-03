# Week 1 Functional-Dependency Analysis and Normalization Notes

## Raw relation
The source file is a single denormalized transaction table with attributes:

`R(InvoiceNo, StockCode, Description, Quantity, InvoiceDate, Price, CustomerID, Country)`

The raw data shows classic retail-order structure: one invoice contains many line items, and one product can appear in many invoices.

## Observed dependency pattern
The dataset strongly suggests these business dependencies:

- `InvoiceNo -> CustomerID, InvoiceDate`
- `CustomerID -> Country`
- `StockCode -> Description`
- `(InvoiceNo, StockCode, Quantity, Price)` is not a stable line identifier because the raw file contains duplicate invoice-product rows.

The raw data also contains quality anomalies that prevent the dependencies from holding perfectly in every row:

- some `Description` values vary for the same `StockCode`
- a small number of `CustomerID` values appear with more than one `Country`
- many rows have missing `CustomerID`
- negative `Quantity` values exist for returns/cancellations

## Normalization result
The Week 1 operational design is decomposed into these relations:

1. `customers(customer_id, country)`
2. `products(stock_code, description)`
3. `invoices(invoice_no, invoice_date, customer_id)`
4. `invoice_items(invoice_item_id, invoice_no, stock_code, quantity, unit_price)`

This removes the major transitive dependencies from the raw table:

- customer country is stored once in `customers`
- product description is stored once in `products`
- invoice header data is stored once in `invoices`
- item-level facts are stored in `invoice_items`

## Keys and constraints
- `customers.customer_id` is the primary key
- `products.stock_code` is the primary key
- `invoices.invoice_no` is the primary key and references `customers`
- `invoice_items.invoice_item_id` is the primary key
- `invoice_items.invoice_no` references `invoices`
- `invoice_items.stock_code` references `products`
- `quantity <> 0`
- `unit_price >= 0`

## Week 1 deliverable status
Done:

- identified the raw denormalized relation
- extracted the likely functional dependencies
- decomposed the data into a normalized operational schema
- updated `schema.sql` to PostgreSQL syntax
- added sample data and basic roles/permissions

Verified CSV facts used in the loader and schema design:

- raw rows: 541,910
- exact duplicate rows: 5,268
- invoices with more than one `InvoiceDate`: 43
- customers with more than one `Country`: 8
- stock codes with several descriptions: 650 raw, 647 after trimming leading/trailing spaces (the loader trims, so 647 is the number it works with)
- stock codes with only null descriptions: 112
- missing `Customer ID` rows: 135,080
- missing `Description` rows: 1,454
- negative `Quantity` rows: 10,624
- negative `Quantity` rows that are not cancellations: 1,336
- negative `Price` rows: 2
- invoice counts by status prefix: 22,061 completed, 3,836 cancelled, 3 adjustments
- the two negative-price adjustment rows are `A563186` and `A563187`
- `Customer ID` values are normalized as strings such as `17850`, not floats like `17850.0`

Loader notes:

- `database/load_data.py` applies the cleaning rules in Python and loads all four operational tables in one transaction (rolled back on any error)
- products get the most frequent non-empty description for their stock code (e.g. `21275` = `ZINC TOP  2 DOOR WOODEN SHELF`, not the one-off `?`)
- without `--reset` the loader refuses to load into non-empty tables; with `--reset` it truncates them first
- `post_clean_duplicates_removed = 3`: after collapsing the 43 multi-timestamp invoices to their earliest date and unifying each product's description, 3 line items became identical to another line in the same invoice (same product, quantity and price) and were merged. They only differed in the raw timestamp or in the spelling of the description, which the operational schema does not store per line.

Load report from a live run of `load_data.py --reset` on the full CSV (PostgreSQL 16):

| Measure | Value |
|---|---|
| raw rows | 541,910 |
| exact duplicates removed | 5,268 |
| post-clean duplicates removed | 3 |
| line items loaded | 536,639 |
| customers | 4,372 |
| products | 4,070 (112 with no description) |
| invoices | 25,900 (22,061 completed, 3,836 cancelled, 3 adjustment) |
| guest invoices (no customer) | 3,710 |

Runtime validation:

- `scripts/validate_db.ps1` (Windows) / `scripts/validate_db.sh` (Linux/macOS) recreate a database, load both schemas and the full CSV, run all query files, integrity checks and five negative tests, and write everything to `docs/validation_output.txt`
- `docs/validation_output.txt` is the live sandbox proof for the report; see `postgres_validation.md`

Still to do later in the project:

- redraw the ER diagram in `database/docs/erd.md` with all columns, and fix the nullable-FK cardinalities (customers -> invoices, agent_sessions -> schema_lookup_log / execution_audit, schema_lookup_log -> execution_audit are zero-or-one on the parent side)
- guardrail logic that uses `policy_rules` and `column_sensitivity` (Week 5)
- transactional execution and audit logging into `execution_audit` / `compensation_ledger` (Weeks 6-8)
- relational-algebra / division query examples