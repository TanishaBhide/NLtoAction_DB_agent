-- Week 1-3 starter SQL queries for the normalized operational schema.
-- These are validation and demo queries for the current project state.

-- 1. Basic projection from a single table.
SELECT customer_id, country
FROM customers
ORDER BY customer_id;

-- 2. Basic selection from products.
SELECT stock_code, description
FROM products
WHERE description ILIKE '%HEART%';

-- 3. Join invoice header with customer data.
SELECT i.invoice_no, i.invoice_date, c.customer_id, c.country
FROM invoices AS i
JOIN customers AS c ON c.customer_id = i.customer_id
ORDER BY i.invoice_date;

-- 4. Join invoice header, line items, and products.
SELECT i.invoice_no, i.invoice_date, p.stock_code, p.description, ii.quantity, ii.unit_price
FROM invoices AS i
JOIN invoice_items AS ii ON ii.invoice_no = i.invoice_no
JOIN products AS p ON p.stock_code = ii.stock_code
ORDER BY i.invoice_no, p.stock_code;

-- 5. Aggregation for invoice size.
SELECT invoice_no, COUNT(*) AS line_count, SUM(quantity) AS total_quantity
FROM invoice_items
GROUP BY invoice_no
ORDER BY line_count DESC, invoice_no;

-- 6. Customer-level invoice summary.
SELECT c.customer_id, c.country, COUNT(i.invoice_no) AS invoice_count
FROM customers AS c
LEFT JOIN invoices AS i ON i.customer_id = c.customer_id
GROUP BY c.customer_id, c.country
ORDER BY invoice_count DESC, c.customer_id;
