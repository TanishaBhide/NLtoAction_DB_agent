CREATE DATABASE online_retail_db;
USE online_retail_db;
CREATE DATABASE IF NOT EXISTS online_retail_db;

USE online_retail_db;
CREATE TABLE CUSTOMER (
    CustomerID VARCHAR(20) PRIMARY KEY,
    Country VARCHAR(100)
);
CREATE TABLE PRODUCT (
    StockCode VARCHAR(50) PRIMARY KEY,
    Description VARCHAR(255)
);
CREATE TABLE INVOICE (
    InvoiceNo VARCHAR(20) PRIMARY KEY,
    InvoiceDate DATETIME,
    CustomerID VARCHAR(20),
    InvoiceStatus VARCHAR(20),

    CONSTRAINT fk_invoice_customer
        FOREIGN KEY (CustomerID)
        REFERENCES CUSTOMER(CustomerID)
);
CREATE TABLE INVOICE_ITEM (
    InvoiceNo VARCHAR(20),
    StockCode VARCHAR(50),
    Quantity INT,
    UnitPrice DECIMAL(10,2),

    CONSTRAINT fk_invoice_item_invoice
        FOREIGN KEY (InvoiceNo)
        REFERENCES INVOICE(InvoiceNo),

    CONSTRAINT fk_invoice_item_product
        FOREIGN KEY (StockCode)
        REFERENCES PRODUCT(StockCode)
);