-- ============================================================
-- Customer Database Initialization
-- ============================================================

-- ------------------------------------------------------------
-- Customers
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customers (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    city VARCHAR(100)
);

-- ------------------------------------------------------------
-- Products
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    id INT AUTO_INCREMENT PRIMARY KEY,
    product_name VARCHAR(100) NOT NULL,
    price DECIMAL(10,2) NOT NULL
);

-- ------------------------------------------------------------
-- Orders
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id INT AUTO_INCREMENT PRIMARY KEY,
    customer_id INT NOT NULL,
    product_id INT NOT NULL,
    quantity INT NOT NULL,

    CONSTRAINT fk_orders_customer
        FOREIGN KEY (customer_id)
        REFERENCES customers(id),

    CONSTRAINT fk_orders_product
        FOREIGN KEY (product_id)
        REFERENCES products(id)
);

-- ------------------------------------------------------------
-- Sample Customers
-- ------------------------------------------------------------
INSERT INTO customers (name, city)
VALUES
    ('Alice', 'New York'),
    ('Bob', 'Chicago'),
    ('Charlie', 'Boston');

-- ------------------------------------------------------------
-- Sample Products
-- ------------------------------------------------------------
INSERT INTO products (product_name, price)
VALUES
    ('Laptop', 1200.00),
    ('Phone', 800.00),
    ('Tablet', 500.00);

-- ------------------------------------------------------------
-- Sample Orders
-- ------------------------------------------------------------
INSERT INTO orders (customer_id, product_id, quantity)
VALUES
    (1, 1, 2),
    (2, 2, 1),
    (3, 3, 4);

-- ------------------------------------------------------------
-- Read-only user for the BI / AI SQL application
-- Do NOT use the MYSQL_USER "customer" for read-only queries.
-- ------------------------------------------------------------
CREATE USER IF NOT EXISTS 'reader'@'%' IDENTIFIED BY 'reader';

GRANT SELECT ON customerdb.* TO 'reader'@'%';

FLUSH PRIVILEGES;