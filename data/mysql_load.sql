-- Edit the folder path to where your CSVs are. Use forward slashes on Windows.
-- Run once in your MySQL client: SET GLOBAL local_infile = 1;  (and connect with --local-infile=1)
USE online_retail_nl2action;

LOAD DATA LOCAL INFILE 'C:/path/to/customer.csv'   INTO TABLE customer
  FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' LINES TERMINATED BY '\n' IGNORE 1 LINES;
LOAD DATA LOCAL INFILE 'C:/path/to/product.csv'    INTO TABLE product
  FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' LINES TERMINATED BY '\n' IGNORE 1 LINES;
LOAD DATA LOCAL INFILE 'C:/path/to/orders.csv'     INTO TABLE orders
  FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' LINES TERMINATED BY '\n' IGNORE 1 LINES;
LOAD DATA LOCAL INFILE 'C:/path/to/order_item.csv' INTO TABLE order_item
  FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' LINES TERMINATED BY '\n' IGNORE 1 LINES;

SELECT 'customer' t, COUNT(*) FROM customer UNION ALL SELECT 'product', COUNT(*) FROM product
UNION ALL SELECT 'orders', COUNT(*) FROM orders UNION ALL SELECT 'order_item', COUNT(*) FROM order_item;
