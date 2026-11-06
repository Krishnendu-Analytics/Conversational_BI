"""AI semantic discovery covers every table, not just the first 20."""

from __future__ import annotations

import json
import re
import threading

from sqlalchemy import select

from app.models import Metric, SchemaTable
from app.services.semantic_discovery import discover_semantic_suggestions
from tests.test_helpers_discovery import make_test_db, seed_connection

TABLES = 40


class _BatchLLM:
    """Answers each batch with an AVG metric for every table in its prompt."""

    client = object()

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    def complete(self, prompt: str) -> str:
        with self._lock:
            self.prompts.append(prompt)
        tables = re.findall(r"main\.(t\d+)\(", prompt)
        if "t13(" in prompt:
            raise RuntimeError("provider hiccup")  # one bad batch must not sink the rest
        return json.dumps(
            {
                "metrics": [
                    {"name": f"avg_qty_{t}", "expression": f"AVG(qty_{t[1:]})"} for t in tables
                ],
                "dimensions": [],
            }
        )


def test_every_table_reaches_the_llm_in_batches() -> None:
    db = make_test_db()
    tenant, conn = seed_connection(db)
    for i in range(TABLES):
        db.add(
            SchemaTable(
                tenant_id=tenant.id,
                connection_id=conn.id,
                schema_name="main",
                table_name=f"t{i:02d}",
                columns_json=json.dumps(
                    {
                        "schema_name": "main",
                        "columns": [
                            {"name": f"qty_{i:02d}", "data_type": "integer", "nullable": True, "is_pk": False}
                        ],
                        "foreign_keys": [],
                    }
                ),
            )
        )
    db.commit()

    llm = _BatchLLM()
    discover_semantic_suggestions(db, conn.id, tenant.id, llm_client=llm)  # type: ignore[arg-type]

    assert len(llm.prompts) == 3  # 15 + 15 + 10
    seen = [t for p in llm.prompts for t in re.findall(r"main\.(t\d+)\(", p)]
    assert sorted(seen) == [f"t{i:02d}" for i in range(TABLES)]

    names = set(db.scalars(select(Metric.name).where(Metric.connection_id == conn.id)).all())
    assert "avg_qty_t39" in names  # a table the old 20-table cut-off never sent
    assert "avg_qty_t20" in names
    assert "avg_qty_t00" not in names  # its batch (with t13) failed; others still applied
