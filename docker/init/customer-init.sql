-- Create Customers table
CREATE TABLE customers (
    customer_id SERIAL PRIMARY KEY,
    customer_name VARCHAR(100) NOT NULL,
    email VARCHAR(100),
    city VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create Products table
CREATE TABLE products (
    product_id SERIAL PRIMARY KEY,
    product_name VARCHAR(100) NOT NULL,
    category VARCHAR(50),
    price DECIMAL(10,2) NOT NULL
);

-- Create Orders table
CREATE TABLE orders (
    order_id SERIAL PRIMARY KEY,
    customer_id INT REFERENCES customers(customer_id),
    product_id INT REFERENCES products(product_id),
    quantity INT NOT NULL,
    order_date DATE DEFAULT CURRENT_DATE
);

-- Sample Customers
INSERT INTO customers (customer_name, email, city) VALUES
('Alice Johnson', 'alice@example.com', 'London'),
('Bob Smith', 'bob@example.com', 'Manchester'),
('Charlie Brown', 'charlie@example.com', 'Birmingham');

-- Sample Products
INSERT INTO products (product_name, category, price) VALUES
('Laptop', 'Electronics', 950.00),
('Keyboard', 'Electronics', 45.00),
('Office Chair', 'Furniture', 180.00);

-- Sample Orders
INSERT INTO orders (customer_id, product_id, quantity) VALUES
(1, 1, 1),
(1, 2, 2),
(2, 3, 1),
(3, 2, 4);

-- Read-only login for the BI app.
-- Do NOT connect as "customer" from the API — that user is a superuser.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'reader') THEN
    CREATE ROLE reader LOGIN PASSWORD 'reader';
  END IF;
END
$$;

GRANT CONNECT ON DATABASE customerdb TO reader;
GRANT USAGE ON SCHEMA public TO reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO reader;