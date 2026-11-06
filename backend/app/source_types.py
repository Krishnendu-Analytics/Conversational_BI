"""Canonical data source types for connections and datasets."""

from __future__ import annotations

from enum import Enum


class SourceType(str, Enum):
    postgres = "postgres"
    mysql = "mysql"
    mssql = "mssql"
    csv = "csv"
    excel = "excel"

    @classmethod
    def is_file_backed(cls, value: str) -> bool:
        return value in {cls.csv.value, cls.excel.value}

    @classmethod
    def is_sql(cls, value: str) -> bool:
        return value in {cls.postgres.value, cls.mysql.value, cls.mssql.value}
