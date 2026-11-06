"""Domain registry: groups a connection's fact tables into named business
domains (Sales, Production, Inventory, ...) for the multi-domain investigator.

Domains are derived, not hand-maintained. ``resolve_domains`` prefers a
curated ``domain`` value from a confirmed Metric/Dimension (see
``app.models.Metric.domain`` / ``Dimension.domain``) when one is set, and
otherwise falls back to the same fact-table scoring and naming convention
whole-database dashboards already use (``pick_fact_tables`` /
``table_domain_label`` in ``app.agent.dashboard``), with known business
abbreviations expanded (``app.agent.domain_taxonomy``) — so there is
always a working, readable default before anyone curates it by hand.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agent.dashboard import pick_fact_tables, table_domain_label
from app.agent.domain_taxonomy import expand_domain_label


class Domain(BaseModel):
    """One investigable business domain: a fact table plus the confirmed
    metrics/dimensions bound to it, if any.

    ``connection_id`` defaults to "" because ``resolve_domains`` itself is
    connection-agnostic — it only ever sees one schema/catalog at a time
    and has no notion of which connection they came from. The caller that
    iterates connections (app.agent.investigator.Planner, for the
    multi-connection case) is what tags each resulting Domain with the
    connection it belongs to.
    """

    name: str
    table: str
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    connection_id: str = ""


def _qualified_table_name(table: dict[str, Any]) -> str | None:
    name = table.get("table_name")
    if not name:
        return None
    schema_name = table.get("schema_name") or ""
    return f"{schema_name}.{name}" if schema_name else str(name)


def _curated_domain_names(
    semantic_catalog: dict[str, Any] | None,
) -> dict[str, str]:
    """Bare table name (lowercase) -> curated domain name, from whichever
    confirmed catalog item names that table and has ``domain`` set. First
    match wins; a table is one domain, not several."""
    mapping: dict[str, str] = {}
    if not semantic_catalog:
        return mapping
    for key in ("metrics", "dimensions"):
        for item in semantic_catalog.get(key) or []:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or "").strip()
            table = str(item.get("physical_table") or "").strip().lower()
            if domain and table and table not in mapping:
                mapping[table] = domain
    return mapping


def _fields_by_table(
    semantic_catalog: dict[str, Any] | None, key: str
) -> dict[str, list[str]]:
    """Bare table name (lowercase) -> confirmed metric/dimension names bound
    to that table, in catalog order."""
    grouped: dict[str, list[str]] = {}
    if not semantic_catalog:
        return grouped
    for item in semantic_catalog.get(key) or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        table = str(item.get("physical_table") or "").strip().lower()
        if not table:
            continue
        grouped.setdefault(table, []).append(str(item["name"]))
    return grouped


def resolve_domains(
    schema: dict[str, Any],
    semantic_catalog: dict[str, Any] | None = None,
    *,
    max_domains: int = 8,
) -> list[Domain]:
    """One ``Domain`` per fact-like table in the schema.

    Reuses ``pick_fact_tables`` (dashboard.py) for table selection/ranking —
    the same logic that already powers whole-database dashboard surveys —
    so this never disagrees with what a dashboard build would consider a
    fact table.
    """
    curated_names = _curated_domain_names(semantic_catalog)
    metrics_by_table = _fields_by_table(semantic_catalog, "metrics")
    dims_by_table = _fields_by_table(semantic_catalog, "dimensions")

    domains: list[Domain] = []
    for table in pick_fact_tables(schema, max_tables=max_domains):
        qualified = _qualified_table_name(table)
        if not qualified:
            continue
        bare = str(table.get("table_name") or "").strip().lower()
        # Curated (human-confirmed) name always wins; otherwise expand any
        # business abbreviation in the auto-derived label ("Ar Txn" ->
        # "Accounts Receivable Txn") rather than showing the raw table-name
        # title-case as-is.
        name = curated_names.get(bare) or expand_domain_label(table_domain_label(table))
        domains.append(
            Domain(
                name=name,
                table=qualified,
                metrics=metrics_by_table.get(bare, []),
                dimensions=dims_by_table.get(bare, []),
            )
        )
    return domains
