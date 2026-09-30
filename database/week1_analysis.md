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
- stock codes with several descriptions: 650
- stock codes with only null descriptions: 112
- missing `Customer ID` rows: 135,080
- missing `Description` rows: 1,454
- negative `Quantity` rows: 10,624
- negative `Quantity` rows that are not cancellations: 1,336
- negative `Price` rows: 2
- invoice counts by status prefix: 22,061 completed, 3,836 cancelled, 3 adjustments
- the two negative-price adjustment rows are `A563186` and `A563187`
- `Customer ID` values are normalized as strings such as `17850`, not floats like `17850.0`

Runtime validation:

- the repo now includes a dedicated Postgres validation note in `postgres_validation.md`
- this workspace does not have `psql`, so the live sandbox proof must be captured on a local PostgreSQL installation before the report is finalized

Loader notes:

- `database/load_data.py` implements the staging-and-cleaning loader rules for the full CSV
- the loader is wired for PostgreSQL transactions and produces a JSON load report when run against a live database

Still to do later in the project:

- write the ER diagram formally
- add the agent-system database schema
- add guardrail/policy tables
- build transaction and audit logging
- add query and division examples