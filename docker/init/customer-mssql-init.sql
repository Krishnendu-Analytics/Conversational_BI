/* ============================================================
   Customer MSSQL Database Initialization Script

   Database: customerdb

   Creates:
   1. customers
   2. products
   3. orders
   4. order_items

   Also inserts sample data for testing.
   ============================================================ */

USE master;
GO


/* ------------------------------------------------------------
   Create the customerdb database if it does not already exist
   ------------------------------------------------------------ */

IF DB_ID(N'customerdb') IS NULL
BEGIN
    CREATE DATABASE customerdb;
    PRINT 'Database customerdb created successfully.';
END
ELSE
BEGIN
    PRINT 'Database customerdb already exists.';
END;
GO


USE customerdb;
GO


/* ------------------------------------------------------------
   Create customers table
   ------------------------------------------------------------ */

IF OBJECT_ID(N'dbo.customers', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.customers
    (
        id INT IDENTITY(1, 1) PRIMARY KEY,

        name NVARCHAR(100) NOT NULL,

        email NVARCHAR(255) NOT NULL UNIQUE,

        phone NVARCHAR(20) NULL,

        city NVARCHAR(100) NULL,

        created_at DATETIME2 NOT NULL
            CONSTRAINT DF_customers_created_at
            DEFAULT SYSUTCDATETIME()
    );

    PRINT 'Table customers created successfully.';
END
ELSE
BEGIN
    PRINT 'Table customers already exists.';
END;
GO


/* ------------------------------------------------------------
   Create products table
   ------------------------------------------------------------ */

IF OBJECT_ID(N'dbo.products', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.products
    (
        id INT IDENTITY(1, 1) PRIMARY KEY,

        name NVARCHAR(150) NOT NULL,

        category NVARCHAR(100) NULL,

        price DECIMAL(12, 2) NOT NULL,

        stock_quantity INT NOT NULL
            CONSTRAINT DF_products_stock_quantity
            DEFAULT 0,

        created_at DATETIME2 NOT NULL
            CONSTRAINT DF_products_created_at
            DEFAULT SYSUTCDATETIME(),

        CONSTRAINT CK_products_price
            CHECK (price >= 0),

        CONSTRAINT CK_products_stock_quantity
            CHECK (stock_quantity >= 0)
    );

    PRINT 'Table products created successfully.';
END
ELSE
BEGIN
    PRINT 'Table products already exists.';
END;
GO


/* ------------------------------------------------------------
   Create orders table
   ------------------------------------------------------------ */

IF OBJECT_ID(N'dbo.orders', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.orders
    (
        id INT IDENTITY(1, 1) PRIMARY KEY,

        customer_id INT NOT NULL,

        order_date DATETIME2 NOT NULL
            CONSTRAINT DF_orders_order_date
            DEFAULT SYSUTCDATETIME(),

        status NVARCHAR(50) NOT NULL
            CONSTRAINT DF_orders_status
            DEFAULT N'pending',

        total_amount DECIMAL(12, 2) NOT NULL
            CONSTRAINT DF_orders_total_amount
            DEFAULT 0,

        CONSTRAINT FK_orders_customers
            FOREIGN KEY (customer_id)
            REFERENCES dbo.customers(id),

        CONSTRAINT CK_orders_total_amount
            CHECK (total_amount >= 0),

        CONSTRAINT CK_orders_status
            CHECK (
                status IN
                (
                    N'pending',
                    N'confirmed',
                    N'shipped',
                    N'delivered',
                    N'cancelled'
                )
            )
    );

    PRINT 'Table orders created successfully.';
END
ELSE
BEGIN
    PRINT 'Table orders already exists.';
END;
GO


/* ------------------------------------------------------------
   Create order_items table
   ------------------------------------------------------------ */

IF OBJECT_ID(N'dbo.order_items', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.order_items
    (
        id INT IDENTITY(1, 1) PRIMARY KEY,

        order_id INT NOT NULL,

        product_id INT NOT NULL,

        quantity INT NOT NULL,

        unit_price DECIMAL(12, 2) NOT NULL,

        CONSTRAINT FK_order_items_orders
            FOREIGN KEY (order_id)
            REFERENCES dbo.orders(id),

        CONSTRAINT FK_order_items_products
            FOREIGN KEY (product_id)
            REFERENCES dbo.products(id),

        CONSTRAINT CK_order_items_quantity
            CHECK (quantity > 0),

        CONSTRAINT CK_order_items_unit_price
            CHECK (unit_price >= 0)
    );

    PRINT 'Table order_items created successfully.';
END
ELSE
BEGIN
    PRINT 'Table order_items already exists.';
END;
GO


/* ------------------------------------------------------------
   Insert sample customers
   ------------------------------------------------------------ */

IF NOT EXISTS (
    SELECT 1
    FROM dbo.customers
    WHERE email = N'rahul@example.com'
)
BEGIN
    INSERT INTO dbo.customers
    (
        name,
        email,
        phone,
        city
    )
    VALUES
    (
        N'Rahul Sharma',
        N'rahul@example.com',
        N'9876543210',
        N'Kolkata'
    );
END;
GO


IF NOT EXISTS (
    SELECT 1
    FROM dbo.customers
    WHERE email = N'priya@example.com'
)
BEGIN
    INSERT INTO dbo.customers
    (
        name,
        email,
        phone,
        city
    )
    VALUES
    (
        N'Priya Das',
        N'priya@example.com',
        N'9876501234',
        N'Mumbai'
    );
END;
GO


IF NOT EXISTS (
    SELECT 1
    FROM dbo.customers
    WHERE email = N'amit@example.com'
)
BEGIN
    INSERT INTO dbo.customers
    (
        name,
        email,
        phone,
        city
    )
    VALUES
    (
        N'Amit Kumar',
        N'amit@example.com',
        N'9123456780',
        N'Delhi'
    );
END;
GO


/* ------------------------------------------------------------
   Insert sample products
   ------------------------------------------------------------ */

IF NOT EXISTS (
    SELECT 1
    FROM dbo.products
    WHERE name = N'Laptop'
)
BEGIN
    INSERT INTO dbo.products
    (
        name,
        category,
        price,
        stock_quantity
    )
    VALUES
    (
        N'Laptop',
        N'Electronics',
        65000.00,
        10
    );
END;
GO


IF NOT EXISTS (
    SELECT 1
    FROM dbo.products
    WHERE name = N'Wireless Mouse'
)
BEGIN
    INSERT INTO dbo.products
    (
        name,
        category,
        price,
        stock_quantity
    )
    VALUES
    (
        N'Wireless Mouse',
        N'Electronics',
        1200.00,
        50
    );
END;
GO


IF NOT EXISTS (
    SELECT 1
    FROM dbo.products
    WHERE name = N'Office Chair'
)
BEGIN
    INSERT INTO dbo.products
    (
        name,
        category,
        price,
        stock_quantity
    )
    VALUES
    (
        N'Office Chair',
        N'Furniture',
        8500.00,
        20
    );
END;
GO


IF NOT EXISTS (
    SELECT 1
    FROM dbo.products
    WHERE name = N'Keyboard'
)
BEGIN
    INSERT INTO dbo.products
    (
        name,
        category,
        price,
        stock_quantity
    )
    VALUES
    (
        N'Keyboard',
        N'Electronics',
        1800.00,
        35
    );
END;
GO


/* ------------------------------------------------------------
   Insert sample orders
   ------------------------------------------------------------ */

DECLARE @RahulCustomerId INT;
DECLARE @PriyaCustomerId INT;
DECLARE @LaptopProductId INT;
DECLARE @MouseProductId INT;
DECLARE @ChairProductId INT;

SELECT @RahulCustomerId = id
FROM dbo.customers
WHERE email = N'rahul@example.com';

SELECT @PriyaCustomerId = id
FROM dbo.customers
WHERE email = N'priya@example.com';

SELECT @LaptopProductId = id
FROM dbo.products
WHERE name = N'Laptop';

SELECT @MouseProductId = id
FROM dbo.products
WHERE name = N'Wireless Mouse';

SELECT @ChairProductId = id
FROM dbo.products
WHERE name = N'Office Chair';


IF NOT EXISTS (
    SELECT 1
    FROM dbo.orders
    WHERE customer_id = @RahulCustomerId
      AND total_amount = 66200.00
)
BEGIN
    INSERT INTO dbo.orders
    (
        customer_id,
        status,
        total_amount
    )
    VALUES
    (
        @RahulCustomerId,
        N'confirmed',
        66200.00
    );

    DECLARE @RahulOrderId INT = SCOPE_IDENTITY();

    INSERT INTO dbo.order_items
    (
        order_id,
        product_id,
        quantity,
        unit_price
    )
    VALUES
    (
        @RahulOrderId,
        @LaptopProductId,
        1,
        65000.00
    );

    INSERT INTO dbo.order_items
    (
        order_id,
        product_id,
        quantity,
        unit_price
    )
    VALUES
    (
        @RahulOrderId,
        @MouseProductId,
        1,
        1200.00
    );
END;


IF NOT EXISTS (
    SELECT 1
    FROM dbo.orders
    WHERE customer_id = @PriyaCustomerId
      AND total_amount = 8500.00
)
BEGIN
    INSERT INTO dbo.orders
    (
        customer_id,
        status,
        total_amount
    )
    VALUES
    (
        @PriyaCustomerId,
        N'delivered',
        8500.00
    );

    DECLARE @PriyaOrderId INT = SCOPE_IDENTITY();

    INSERT INTO dbo.order_items
    (
        order_id,
        product_id,
        quantity,
        unit_price
    )
    VALUES
    (
        @PriyaOrderId,
        @ChairProductId,
        1,
        8500.00
    );
END;
GO


/* ------------------------------------------------------------
   Display the initialized data
   ------------------------------------------------------------ */

PRINT 'Customer MSSQL database initialization completed.';
GO

/* ------------------------------------------------------------
   Read-only login for the BI app (do not use sa from the API)
   ------------------------------------------------------------ */

IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'reader')
BEGIN
-- Password must satisfy SQL Server policy (>=8 chars, complexity).
CREATE LOGIN reader WITH PASSWORD = N'Reader1234!';
END
GO

USE customerdb;
GO

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'reader')
BEGIN
    CREATE USER reader FOR LOGIN reader;
END
GO

ALTER ROLE db_datareader ADD MEMBER reader;
GO

SELECT *
FROM dbo.customers;
GO

SELECT *
FROM dbo.products;
GO

SELECT *
FROM dbo.orders;
GO

SELECT *
FROM dbo.order_items;
GO