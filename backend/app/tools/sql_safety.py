"""Check SQL is safe before running it."""

import sqlglot
from sqlglot import exp

from app.config import get_settings


def _sqlglot_dialect(db_type: str | None) -> str | None:
    """Map platform db_type to a sqlglot read/write dialect."""
    from app.sql_dialect import safety_dialect

    return safety_dialect(db_type)


def _parse_sql(sql: str, dialect: str | None) -> exp.Expression:
    if dialect:
        return sqlglot.parse_one(sql, read=dialect)
    return sqlglot.parse_one(sql)


def _parse_all_sql(sql: str, dialect: str | None) -> list[exp.Expression]:
    if dialect:
        statements = sqlglot.parse(sql, read=dialect)
    else:
        statements = sqlglot.parse(sql)
    return [stmt for stmt in statements if stmt is not None]


_BLOCKED_LEADING_KEYWORDS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "CREATE",
        "ALTER",
        "DROP",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "COMMENT",
        "VACUUM",
        "CALL",
        "EXEC",
        "EXECUTE",
        "COPY",
        "ANALYZE",
        "REINDEX",
        "CLUSTER",
        "REFRESH",
        "REPLACE",
        "LOAD",
        "IMPORT",
        "EXPORT",
        "BACKUP",
        "RESTORE",
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
        "DECLARE",
        "USE",
    }
)

_MUTATION_EXPRESSION_TYPES: tuple[type[exp.Expression], ...] = tuple(
    cls
    for cls in (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Merge,
        exp.Create,
        exp.Drop,
        getattr(exp, "AlterTable", None),
        getattr(exp, "Alter", None),
        exp.Command,
        exp.Comment,
        exp.Transaction,
        exp.Into,
        getattr(exp, "Copy", None),
        getattr(exp, "Replace", None),
    )
    if cls is not None
)


def _leading_sql_keyword(sql: str) -> str:
    text = sql.strip()
    while text:
        if text.startswith("--"):
            text = text.split("\n", 1)[-1].strip() if "\n" in text else ""
            continue
        if text.startswith("/*"):
            end = text.find("*/")
            if end == -1:
                return ""
            text = text[end + 2 :].strip()
            continue
        if text[0] in "(\n\r\t ":
            text = text[1:].lstrip()
            continue
        break
    word: list[str] = []
    for ch in text:
        if ch.isalnum() or ch == "_":
            word.append(ch)
        else:
            break
    return "".join(word).upper()


class SyntaxValidator:
    """Reject invalid SQL."""

    def __init__(self, dialect: str | None = None) -> None:
        self.dialect = dialect

    def validate(self, sql: str) -> str:
        try:
            _parse_sql(sql, self.dialect)
        except Exception as exc:
            raise ValueError("Invalid SQL syntax") from exc

        return sql


class StatementValidator:
    """Allow only SELECT (including UNION of SELECTs)."""

    def __init__(self, dialect: str | None = None) -> None:
        self.dialect = dialect

    @staticmethod
    def _is_select_query(statement: exp.Expression) -> bool:
        if isinstance(statement, exp.Select):
            return True
        if isinstance(statement, exp.With):
            return isinstance(statement.this, exp.Select)
        if isinstance(statement, exp.Union):
            return StatementValidator._is_select_query(
                statement.left
            ) and StatementValidator._is_select_query(statement.right)
        return False

    @staticmethod
    def _contains_mutation(statement: exp.Expression) -> bool:
        return any(statement.find(cls) is not None for cls in _MUTATION_EXPRESSION_TYPES)

    def validate(self, sql: str) -> str:
        keyword = _leading_sql_keyword(sql)
        if keyword in _BLOCKED_LEADING_KEYWORDS:
            raise ValueError("Only SELECT statements are allowed")

        try:
            statements = _parse_all_sql(sql, self.dialect)
        except Exception as exc:
            raise ValueError("Invalid SQL syntax") from exc

        if len(statements) != 1:
            raise ValueError("Only a single SELECT statement is allowed")

        statement = statements[0]
        if self._contains_mutation(statement):
            raise ValueError("Only SELECT statements are allowed")

        if self._is_select_query(statement):
            return sql

        raise ValueError("Only SELECT statements are allowed")


class SchemaValidator:
    """Check tables/columns exist (allow SELECT aliases + CTEs)."""

    def __init__(
        self,
        schema: dict | None = None,
        dialect: str | None = None,
    ):
        self.schema = schema or {}
        self.dialect = dialect

    @staticmethod
    def _select_aliases(statement: exp.Expression) -> set[str]:
        """Explicit AS aliases only (not bare column names)."""
        aliases: set[str] = set()
        for select in statement.find_all(exp.Select):
            for projected in select.expressions:
                # Bare columns must still be schema-checked.
                if isinstance(projected, exp.Alias) and projected.alias:
                    aliases.add(str(projected.alias).lower())
        return aliases

    @staticmethod
    def _select_projection_column_ids(statement: exp.Expression) -> set[int]:
        """Identity of every Column node that is part of a SELECT-list
        projected expression (e.g. the ``supplier`` in ``supplier AS
        supplier``). These are the actual source references and must
        always be schema-checked — never skipped just because their name
        happens to equal their own (or some other expression's) alias."""
        ids: set[int] = set()
        for select in statement.find_all(exp.Select):
            for projected in select.expressions:
                for col in projected.find_all(exp.Column):
                    ids.add(id(col))
        return ids

    @staticmethod
    def _cte_names(statement: exp.Expression) -> set[str]:
        names: set[str] = set()
        for cte in statement.find_all(exp.CTE):
            alias = cte.alias
            if alias:
                names.add(str(alias).lower())
        return names

    @staticmethod
    def _derived_table_outputs(statement: exp.Expression) -> dict[str, set[str] | None]:
        """Alias → output column names of each derived table
        (``FROM (SELECT ... AS actual_qty ...) s``); None when it selects *.

        Comparing measures from two tables means aggregating each in its own
        subquery and joining the results — ``s.actual_qty`` is then a real
        column of ``s``, not of any schema table. The subqueries' own
        columns are still checked against the schema like any other."""
        outputs: dict[str, set[str] | None] = {}
        for sub in statement.find_all(exp.Subquery):
            alias = sub.alias
            if not alias:
                continue
            inner = sub.this
            select = inner if isinstance(inner, exp.Select) else inner.find(exp.Select)
            if select is None:
                continue
            names: set[str] | None = set()
            for projected in select.expressions:
                if isinstance(projected, exp.Star) or (
                    isinstance(projected, exp.Column) and projected.name == "*"
                ):
                    names = None
                    break
                if projected.alias_or_name:
                    names.add(str(projected.alias_or_name).lower())
            outputs[str(alias).lower()] = names
        return outputs

    @staticmethod
    def _table_alias_to_name(statement: exp.Expression) -> dict[str, str]:
        """Map FROM alias → physical table (schema-qualified when present)."""
        mapping: dict[str, str] = {}
        for table in statement.find_all(exp.Table):
            name = table.name
            if not name:
                continue
            schema_part = str(getattr(table, "db", None) or "").lower()
            bare = str(name).lower()
            physical = f"{schema_part}.{bare}" if schema_part else bare
            mapping[bare] = physical
            mapping[physical] = physical
            alias = table.alias
            if alias:
                mapping[str(alias).lower()] = physical
        return mapping

    def validate(self, sql: str) -> str:
        # If schema is not provided, skip schema validation.
        if not self.schema:
            return sql

        statement = _parse_sql(sql, self.dialect)

        valid_tables: dict[str, set[str]] = {}
        bare_owners: dict[str, str] = {}
        raw_tables = self.schema.get("tables")
        if isinstance(raw_tables, list):
            for table in raw_tables:
                if not isinstance(table, dict):
                    continue
                table_name = table.get("table_name")
                cols = table.get("columns") or []
                if not table_name or not isinstance(cols, list):
                    continue
                col_names: set[str] = set()
                for col in cols:
                    if isinstance(col, dict) and col.get("name"):
                        col_names.add(str(col["name"]).lower())
                schema_name = str(table.get("schema_name") or "").lower()
                bare = str(table_name).lower()
                qualified = f"{schema_name}.{bare}" if schema_name else bare
                valid_tables[qualified] = col_names
                # Allow bare table name only when unambiguous across schemas.
                if bare not in bare_owners:
                    bare_owners[bare] = qualified
                    valid_tables[bare] = col_names
                elif bare_owners[bare] != qualified:
                    valid_tables.pop(bare, None)

        if not valid_tables:
            raise ValueError("Schema validation failed: no tables available")

        select_aliases = self._select_aliases(statement)
        projection_column_ids = self._select_projection_column_ids(statement)
        cte_names = self._cte_names(statement)
        derived_outputs = self._derived_table_outputs(statement)
        alias_to_table = self._table_alias_to_name(statement)

        for table in statement.find_all(exp.Table):
            table_name = table.name
            if not table_name:
                continue
            schema_part = str(getattr(table, "db", None) or "").lower()
            name_lc = str(table_name).lower()
            qualified = f"{schema_part}.{name_lc}" if schema_part else name_lc
            if name_lc in cte_names or qualified in cte_names:
                continue
            if qualified not in valid_tables and name_lc not in valid_tables:
                label = f"{schema_part}.{table_name}" if schema_part else table_name
                raise ValueError(f"Table '{label}' does not exist")

        for column in statement.find_all(exp.Column):
            column_name = column.name

            if column_name == "*":
                continue

            column_lc = str(column_name).lower()

            if column_lc in select_aliases and id(column) not in projection_column_ids:
                continue

            qualified_table = getattr(column, "table", None)
            if qualified_table:
                qualified_lc = str(qualified_table).lower()
                # column may be schema.table.col — sqlglot puts table in .table
                if qualified_lc in cte_names:
                    continue
                if qualified_lc in derived_outputs and qualified_lc not in alias_to_table:
                    outputs = derived_outputs[qualified_lc]
                    if outputs is None or column_lc in outputs:
                        continue
                    raise ValueError(
                        f"Column '{qualified_table}.{column_name}' does not exist"
                    )
                physical = alias_to_table.get(qualified_lc, qualified_lc)
                if physical in cte_names:
                    continue
                cols = valid_tables.get(physical)
                if cols is None:
                    # Try matching any *.physical
                    for key, value in valid_tables.items():
                        if key == physical or key.endswith(f".{physical}"):
                            cols = value
                            break
                if cols is not None:
                    if column_lc not in cols:
                        raise ValueError(
                            f"Column '{qualified_table}.{column_name}' does not exist"
                        )
                    continue
                raise ValueError(
                    f"Column '{qualified_table}.{column_name}' does not exist"
                )

            if not any(column_lc in cols for cols in valid_tables.values()):
                raise ValueError(f"Column '{column_name}' does not exist")

        return sql


class ExecutionGuard:
    """Timeout + row limit."""

    def __init__(
        self,
        max_rows: int = 10000,
        timeout_seconds: int = 15,
        dialect: str | None = None,
    ):
        self.max_rows = max_rows
        self.timeout_seconds = timeout_seconds
        self.dialect = dialect

    def validate(self, sql: str) -> str:
        statement = _parse_sql(sql, self.dialect)

        # Add LIMIT if query does not already have one.
        # For `WITH`, apply LIMIT to the main SELECT.
        if isinstance(statement, exp.With) and isinstance(statement.this, exp.Select):
            select_stmt = statement.this
        else:
            select_stmt = statement

        if self.dialect == "postgres":
            _cast_round_arguments_to_numeric(statement)

        if isinstance(select_stmt, exp.Select) and select_stmt.args.get("limit") is None:
            if self.dialect == "tsql":
                select_stmt = select_stmt.limit(
                    self.max_rows,
                    dialect="tsql",
                )
            else:
                select_stmt = select_stmt.limit(self.max_rows)
            if isinstance(statement, exp.With):
                statement.set("this", select_stmt)
            else:
                statement = select_stmt

        if self.dialect:
            return statement.sql(dialect=self.dialect)
        return statement.sql()


_NUMERIC_TYPES = {exp.DataType.Type.DECIMAL, exp.DataType.Type.BIGDECIMAL}


def _cast_round_arguments_to_numeric(statement: exp.Expression) -> None:
    """Postgres has no ROUND(double precision, integer): with any float
    column (real/double) inside, ``ROUND(SUM(x) * 100.0 / ..., 2)`` fails at
    run time — and a failed query used to be swapped for an unrelated
    fallback answer. Casting the argument to NUMERIC is always valid."""
    for node in list(statement.find_all(exp.Round)):
        if node.args.get("decimals") is None:
            continue
        arg = node.this
        if isinstance(arg, exp.Cast) and arg.to.this in _NUMERIC_TYPES:
            continue
        node.set("this", exp.Cast(this=arg, to=exp.DataType.build("NUMERIC")))


class SQLSafetyPipeline:
    """Run all checks in order."""

    def __init__(
        self,
        schema: dict | None = None,
        db_type: str | None = None,
        *,
        max_rows: int | None = None,
        timeout_seconds: int | None = None,
    ):
        dialect = _sqlglot_dialect(db_type)
        settings = get_settings()
        self.syntax_validator = SyntaxValidator(dialect=dialect)
        self.statement_validator = StatementValidator(dialect=dialect)
        self.schema_validator = SchemaValidator(schema, dialect=dialect)
        self.execution_guard = ExecutionGuard(
            max_rows=max_rows if max_rows is not None else settings.query_max_rows,
            timeout_seconds=(
                timeout_seconds
                if timeout_seconds is not None
                else settings.query_timeout_seconds
            ),
            dialect=dialect,
        )

    def validate(self, sql: str) -> str:
        sql = self.syntax_validator.validate(sql)
        sql = self.statement_validator.validate(sql)
        sql = self.schema_validator.validate(sql)
        sql = self.execution_guard.validate(sql)

        return sql


def assert_read_only_sql(sql: str, *, db_type: str | None = None) -> str:
    """Final SELECT-only guard at the SQL execution boundary.

    Complements SQLSafetyPipeline (syntax/schema/LIMIT). Callers must still
    run the full pipeline for user/AI SQL; this function prevents mutation
    statements from reaching the customer database if a path skips it.
    """
    return StatementValidator(dialect=_sqlglot_dialect(db_type)).validate(sql)
