CREATE DATABASE IF NOT EXISTS online_retail_nl2action;
USE online_retail_nl2action;

CREATE TABLE customer (
  customer_id INT PRIMARY KEY,
  name        VARCHAR(100) NOT NULL,
  email       VARCHAR(150) NOT NULL UNIQUE,
  region      VARCHAR(50)  NOT NULL
) ENGINE=InnoDB;

CREATE TABLE product (
  product_id   INT PRIMARY KEY,
  stock_code   VARCHAR(20) NOT NULL UNIQUE,
  product_name VARCHAR(150) NOT NULL,
  category     VARCHAR(100) NOT NULL,
  price        DECIMAL(10,2) NOT NULL,
  CHECK (price >= 0)
) ENGINE=InnoDB;

CREATE TABLE orders (
  order_id    INT PRIMARY KEY,
  invoice_no  VARCHAR(20) NOT NULL UNIQUE,
  customer_id INT NOT NULL,
  order_date  DATE NOT NULL,
  status      VARCHAR(20) NOT NULL,
  CHECK (status IN ('Completed','Shipped','Pending','Cancelled')),
  FOREIGN KEY (customer_id) REFERENCES customer(customer_id),
  INDEX idx_orders_date (order_date)
) ENGINE=InnoDB;

CREATE TABLE order_item (
  order_id   INT NOT NULL,
  product_id INT NOT NULL,
  quantity   INT NOT NULL,
  unit_price DECIMAL(10,2) NOT NULL,
  PRIMARY KEY (order_id, product_id),
  CHECK (quantity > 0),
  FOREIGN KEY (order_id)   REFERENCES orders(order_id),
  FOREIGN KEY (product_id) REFERENCES product(product_id),
  INDEX idx_item_product (product_id)
) ENGINE=InnoDB;
