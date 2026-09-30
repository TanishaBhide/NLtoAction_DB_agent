-- Real sample rows from database_raw.csv.

INSERT INTO customers (customer_id, country) VALUES
    ('17850', 'United Kingdom'),
    ('12494', 'France'),
    ('14527', 'United Kingdom');

INSERT INTO products (stock_code, description) VALUES
    ('85123A', 'WHITE HANGING HEART T-LIGHT HOLDER'),
    ('71053', 'WHITE METAL LANTERN'),
    ('84406B', 'CREAM CUPID HEARTS COAT HANGER'),
    ('84029G', 'KNITTED UNION FLAG HOT WATER BOTTLE'),
    ('84029E', 'RED WOOLLY HOTTIE WHITE HEART.'),
    ('22752', 'SET 7 BABUSHKA NESTING BOXES'),
    ('21730', 'GLASS STAR FROSTED T-LIGHT HOLDER'),
    ('21843', 'RED RETROSPOT CAKE STAND'),
    ('22667', 'RECIPE BOX RETROSPOT '),
    ('POST', 'POSTAGE'),
    ('22139', NULL),
    ('D', 'Discount'),
    ('B', 'Adjust bad debt');

INSERT INTO invoices (invoice_no, invoice_date, customer_id, status) VALUES
    ('536365', '2010-12-01 08:26:00', '17850', 'completed'),
    ('537967', '2010-12-09 11:31:00', '12494', 'completed'),
    ('536414', '2010-12-01 11:52:00', NULL, 'completed'),
    ('C536379', '2010-12-01 09:41:00', '14527', 'cancelled'),
    ('A563186', '2011-08-12 14:51:00', NULL, 'adjustment');

INSERT INTO invoice_items (invoice_no, stock_code, quantity, unit_price) VALUES
    ('536365', '85123A', 6, 2.55),
    ('536365', '71053', 6, 3.39),
    ('536365', '84406B', 8, 2.75),
    ('536365', '84029G', 6, 3.39),
    ('536365', '84029E', 6, 3.39),
    ('536365', '22752', 2, 7.65),
    ('536365', '21730', 6, 4.25),
    ('537967', '21843', 2, 10.95),
    ('537967', '22667', 6, 2.95),
    ('537967', 'POST', 1, 18.00),
    ('536414', '22139', 56, 0.00),
    ('C536379', 'D', -1, 27.50),
    ('A563186', 'B', 1, -11062.06);