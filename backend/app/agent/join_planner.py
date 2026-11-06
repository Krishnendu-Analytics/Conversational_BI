"""Deterministic JOIN planning from discovered FKs and confirmed relationships."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class JoinEdge:
    left_table: str
    right_table: str
    condition: str


class JoinAmbiguityError(Exception):
    """Raised when multiple valid join paths exist without a confirmed relationship."""

    def __init__(self, message: str, candidates: list[list[JoinEdge]] | None = None) -> None:
        super().__init__(message)
        self.candidates = candidates or []


def _normalize_table_key(schema_name: str | None, table_name: str) -> str:
    if schema_name and not table_name.startswith(f"{schema_name}."):
        return f"{schema_name}.{table_name.split('.')[-1]}"
    return table_name


def _build_fk_graph(schema: dict[str, Any]) -> dict[str, list[JoinEdge]]:
    bare_to_qualified: dict[str, str] = {}
    for table in schema.get("tables") or []:
        if not isinstance(table, dict):
            continue
        key = _normalize_table_key(
            table.get("schema_name"), str(table.get("table_name") or "")
        )
        bare = str(table.get("table_name") or "").split(".")[-1]
        bare_to_qualified[bare] = key
        bare_to_qualified[key] = key

    def _resolve_ref(ref: str, from_schema: str | None) -> str:
        if "." in ref:
            return ref
        if from_schema:
            candidate = f"{from_schema}.{ref}"
            if candidate in bare_to_qualified:
                return candidate
        return bare_to_qualified.get(ref, ref)

    graph: dict[str, list[JoinEdge]] = {}
    for table in schema.get("tables") or []:
        if not isinstance(table, dict):
            continue
        from_schema = table.get("schema_name")
        from_key = _normalize_table_key(from_schema, str(table.get("table_name") or ""))
        for fk in table.get("foreign_keys") or []:
            if not isinstance(fk, dict):
                continue
            ref_schema = fk.get("referred_schema") or from_schema
            ref_table = fk.get("referred_table") or ""
            to_key = _resolve_ref(str(ref_table), ref_schema)
            col = fk.get("column")
            ref_col = fk.get("referred_column")
            if not col or not ref_col or not to_key:
                continue
            condition = f"{from_key}.{col} = {to_key}.{ref_col}"
            edge = JoinEdge(from_key, to_key, condition)
            rev = JoinEdge(to_key, from_key, f"{to_key}.{ref_col} = {from_key}.{col}")
            graph.setdefault(from_key, []).append(edge)
            graph.setdefault(to_key, []).append(rev)
    return graph


def _add_confirmed_relationships(
    graph: dict[str, list[JoinEdge]],
    relationships: list[dict[str, Any]],
) -> None:
    for rel in relationships:
        if not isinstance(rel, dict):
            continue
        if str(rel.get("status") or "") != "confirmed":
            continue
        fk_raw = rel.get("source_fk_json") or rel.get("source_fk")
        if isinstance(fk_raw, str):
            try:
                fk = json.loads(fk_raw)
            except json.JSONDecodeError:
                continue
        elif isinstance(fk_raw, dict):
            fk = fk_raw
        else:
            continue
        from_table = fk.get("from_table") or fk.get("from_schema_table")
        to_table = fk.get("referred_table")
        col = fk.get("column")
        ref_col = fk.get("referred_column")
        if not all([from_table, to_table, col, ref_col]):
            continue
        condition = f"{from_table}.{col} = {to_table}.{ref_col}"
        edge = JoinEdge(str(from_table), str(to_table), condition)
        rev = JoinEdge(str(to_table), str(from_table), f"{to_table}.{ref_col} = {from_table}.{col}")
        # A confirmed relationship often repeats an FK already in the graph;
        # a second identical edge would look like an ambiguous join path.
        if edge in graph.get(edge.left_table, []):
            continue
        graph.setdefault(edge.left_table, []).append(edge)
        graph.setdefault(rev.left_table, []).append(rev)


def _shortest_paths(
    graph: dict[str, list[JoinEdge]], start: str, goal: str
) -> list[list[JoinEdge]]:
    if start == goal:
        return [[]]
    queue: deque[tuple[str, list[JoinEdge]]] = deque([(start, [])])
    found: list[list[JoinEdge]] = []
    min_len: int | None = None

    while queue:
        node, path = queue.popleft()
        if min_len is not None and len(path) > min_len:
            break
        for edge in graph.get(node, []):
            if any(e.right_table == edge.left_table and e.left_table == edge.right_table for e in path):
                continue
            next_path = path + [edge]
            if edge.right_table == goal:
                if min_len is None or len(next_path) < min_len:
                    min_len = len(next_path)
                    found = [next_path]
                elif len(next_path) == min_len:
                    found.append(next_path)
            else:
                queue.append((edge.right_table, next_path))
    return found


def tables_from_expression(expr: str, schema: dict[str, Any] | None = None) -> set[str]:
    import re

    tokens = set(re.findall(r"([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*)", expr or ""))
    if schema is None:
        return tokens

    known_qualified: set[str] = set()
    bare_to_qualified: dict[str, str] = {}
    for table in schema.get("tables") or []:
        if not isinstance(table, dict):
            continue
        key = _normalize_table_key(
            table.get("schema_name"), str(table.get("table_name") or "")
        )
        known_qualified.add(key)
        bare = str(table.get("table_name") or "").split(".")[-1]
        if bare not in bare_to_qualified:
            bare_to_qualified[bare] = key

    resolved: set[str] = set()
    for token in tokens:
        if token in known_qualified:
            resolved.add(token)
            continue
        if "." in token:
            bare_table, _col = token.split(".", 1)
            if bare_table in bare_to_qualified:
                resolved.add(bare_to_qualified[bare_table])
    return resolved


def build_join_clauses(
    required_tables: set[str],
    schema: dict[str, Any],
    confirmed_relationships: list[dict[str, Any]] | None = None,
    *,
    base_table: str | None = None,
) -> tuple[str, list[str]]:
    """Return (FROM with JOINs, list of join condition strings)."""
    if not required_tables:
        raise ValueError("No tables required")
    if len(required_tables) == 1:
        return next(iter(required_tables)), []

    graph = _build_fk_graph(schema)
    _add_confirmed_relationships(graph, confirmed_relationships or [])

    anchor = base_table or sorted(required_tables)[0]
    if anchor not in required_tables:
        anchor = sorted(required_tables)[0]

    all_edges: list[JoinEdge] = []
    remaining = set(required_tables) - {anchor}

    while remaining:
        best_path: list[JoinEdge] | None = None
        best_target: str | None = None
        all_candidates: list[list[JoinEdge]] = []

        for target in remaining:
            paths = _shortest_paths(graph, anchor, target)
            if not paths:
                continue
            if len(paths) > 1:
                raise JoinAmbiguityError(
                    f"Multiple join paths from {anchor} to {target} without confirmed relationship.",
                    candidates=paths,
                )
            path = paths[0]
            if best_path is None or len(path) < len(best_path):
                best_path = path
                best_target = target
            all_candidates.extend(paths)

        if best_path is None or best_target is None:
            raise JoinAmbiguityError(
                f"No FK path to join required tables: {sorted(required_tables)}"
            )

        for edge in best_path:
            if edge not in all_edges:
                all_edges.append(edge)
        remaining.remove(best_target)

    from_clause = anchor
    join_sql: list[str] = []
    joined = {anchor}
    for edge in all_edges:
        if edge.right_table not in joined:
            from_clause += f" JOIN {edge.right_table} ON {edge.condition}"
            joined.add(edge.right_table)
            joined.add(edge.left_table)
            join_sql.append(edge.condition)
        elif edge.left_table not in joined:
            from_clause += f" JOIN {edge.left_table} ON {edge.condition}"
            joined.add(edge.left_table)
            join_sql.append(edge.condition)

    return from_clause, join_sql
