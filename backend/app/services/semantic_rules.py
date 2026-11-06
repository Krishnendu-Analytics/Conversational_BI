"""Business rules, agent settings and vocabulary for a connection.

A semantic-model workbook usually carries guidance that isn't a formula or
a join: "default revenue is net_sales_value", "never sum daily inventory
balances", "vendor means supplier". This module:

1. ``parse_rule_sheets`` reads three sheet shapes by their headers:
   rules (a rule column, optional id / category / guardrail), settings
   (parameter + value, optional rule) and vocabulary (a concept + its
   synonyms).
2. ``check_rule`` warns when a rule names a table or column
   (``fact_sales.net_sales_value``, ``dim_date``) this connection doesn't
   have — such a rule would steer the agent toward something that isn't there.
3. ``format_rules_for_prompt`` renders them for the SQL prompts, and
   ``expand_question`` adds a concept's name to a question that uses one of
   its synonyms, so schema ranking finds the right table.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SemanticRule
from app.services.file_import import clean_cell, norm_header

KINDS = ("rule", "setting", "vocabulary")
MAX_RULE_LEN = 2000
MAX_TERMS = 30

# Prompt budget: rules are short, but a large workbook shouldn't crowd out
# the schema.
_PROMPT_RULES = 60
_PROMPT_VOCABULARY = 60
_PROMPT_TEXT_CHARS = 300

_RULE_HEADERS = {
    "business rule", "rule", "rule text", "rule description", "guideline",
    "policy", "agent rule", "instruction", "constraint",
}
_RULE_KEY_HEADERS = {"rule id", "id", "rule no", "rule number", "rule code", "code"}
_SETTING_KEY_HEADERS = {"parameter", "setting", "config", "configuration", "option", "key"}
_SETTING_VALUE_HEADERS = {"value", "setting value", "default", "default value"}
_CATEGORY_HEADERS = {"category", "rule category", "area", "domain", "type", "topic"}
_GUIDANCE_WORDS = ("guardrail", "guidance", "note", "action", "exception", "rationale")
_CONCEPT_HEADERS = {
    "concept", "term", "business term", "word", "keyword", "canonical term",
    "standard term", "business concept",
}
_SYNONYM_WORDS = ("synonym", "alias", "also known as", "alternate", "alternative")


@dataclass
class RuleRow:
    kind: str  # rule | setting | vocabulary
    name: str = ""  # rule id, parameter or concept
    category: str = ""
    rule: str = ""  # rule text; setting value line; vocabulary concept
    guidance: str = ""
    terms: list[str] = field(default_factory=list)
    origin: str = "import"  # import | ai
    row: int | None = None

    @property
    def key(self) -> str:
        return rule_key(self.kind, self.name, self.rule)


def rule_key(kind: str, name: str, rule: str) -> str:
    base = name if name else rule
    if kind == "vocabulary":
        base = rule or name
    return " ".join(base.lower().split())[:200]


def _index(headers: list[str], exact: set[str], taken: set[int]) -> int | None:
    return next((i for i, h in enumerate(headers) if h in exact and i not in taken), None)


def _containing(headers: list[str], words: Iterable[str], taken: set[int]) -> int | None:
    return next(
        (i for i, h in enumerate(headers) if i not in taken and h and any(w in h for w in words)),
        None,
    )


def _sheet_header(rows: list[list[object]]) -> tuple[int, str, dict[str, int]] | None:
    for idx, row in enumerate(rows[:10]):
        headers = [norm_header(c) for c in row]
        taken: set[int] = set()

        concept = _index(headers, _CONCEPT_HEADERS, taken)
        synonyms = _containing(headers, _SYNONYM_WORDS, {concept} if concept is not None else set())
        if concept is not None and synonyms is not None:
            return idx, "vocabulary", {"concept": concept, "terms": synonyms}

        setting = _index(headers, _SETTING_KEY_HEADERS, taken)
        value = _index(headers, _SETTING_VALUE_HEADERS, taken)
        if setting is not None and value is not None:
            cols = {"name": setting, "value": value}
            taken = {setting, value}
            rule = _index(headers, _RULE_HEADERS, taken)
            if rule is None:
                rule = _containing(headers, ("rule", "description", "meaning"), taken)
            if rule is not None:
                cols["rule"] = rule
            return idx, "setting", cols

        rule = _index(headers, _RULE_HEADERS, taken)
        if rule is not None:
            cols = {"rule": rule}
            taken = {rule}
            for key, found in (
                ("name", _index(headers, _RULE_KEY_HEADERS, taken)),
                ("category", _index(headers, _CATEGORY_HEADERS, taken)),
            ):
                if found is not None:
                    cols[key] = found
                    taken.add(found)
            guidance = _containing(headers, _GUIDANCE_WORDS, taken)
            if guidance is not None:
                cols["guidance"] = guidance
            return idx, "rule", cols
    return None


def split_terms(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for part in re.split(r"[;,|/\n]", text or ""):
        term = " ".join(part.split())
        if term and term.lower() not in seen and len(term) <= 120:
            seen.add(term.lower())
            out.append(term)
    return out[:MAX_TERMS]


def parse_rule_sheets(
    sheets: list[list[list[object]]],
) -> tuple[list[RuleRow], list[tuple[int | None, str]], set[int]]:
    """Return (rows, skipped, indexes of the sheets that were used)."""
    rows_out: list[RuleRow] = []
    skipped: list[tuple[int | None, str]] = []
    used: set[int] = set()
    seen: set[tuple[str, str]] = set()

    for sheet_index, rows in enumerate(sheets):
        header = _sheet_header(rows)
        if header is None:
            continue
        used.add(sheet_index)
        start, kind, cols = header

        def cell(row: list[object], name: str) -> str:
            i = cols.get(name)
            return clean_cell(row[i]) if i is not None and i < len(row) else ""

        for idx in range(start + 1, len(rows)):
            row = rows[idx]
            if not any(clean_cell(c) for c in row):
                continue
            row_no = idx + 1
            if kind == "vocabulary":
                concept = cell(row, "concept")
                terms = [t for t in split_terms(cell(row, "terms")) if t.lower() != concept.lower()]
                if not concept or not terms:
                    skipped.append((row_no, "vocabulary row needs a term and its synonyms"))
                    continue
                item = RuleRow(kind, name=concept[:200], rule=concept[:200], terms=terms, row=row_no)
            elif kind == "setting":
                name, value = cell(row, "name"), cell(row, "value")
                if not name or not value:
                    skipped.append((row_no, "setting row needs a parameter and a value"))
                    continue
                item = RuleRow(
                    kind,
                    name=name[:200],
                    rule=f"{name} = {value}"[:MAX_RULE_LEN],
                    guidance=cell(row, "rule")[:MAX_RULE_LEN],
                    row=row_no,
                )
            else:
                text = cell(row, "rule")
                if not text:
                    skipped.append((row_no, "rule row has no rule text"))
                    continue
                if len(text) > MAX_RULE_LEN:
                    skipped.append((row_no, f"rule longer than {MAX_RULE_LEN} characters"))
                    continue
                item = RuleRow(
                    kind,
                    name=cell(row, "name")[:200],
                    category=cell(row, "category")[:120],
                    rule=text,
                    guidance=cell(row, "guidance")[:MAX_RULE_LEN],
                    row=row_no,
                )
            if (item.kind, item.key) in seen:
                continue
            seen.add((item.kind, item.key))
            rows_out.append(item)
    return rows_out, skipped, used


# --- Checking against the schema --------------------------------------------------------

_DOTTED_RE = re.compile(r"\b([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)\b")
_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]*[a-z0-9]\b")


def _schema_names(schema: dict[str, Any]) -> tuple[set[str], set[str]]:
    tables: set[str] = set()
    columns: set[str] = set()
    for t in schema.get("tables") or []:
        if not isinstance(t, dict) or not t.get("table_name"):
            continue
        tables.add(str(t["table_name"]).lower())
        if t.get("schema_name"):
            tables.add(str(t["schema_name"]).lower())
        for c in t.get("columns") or []:
            if isinstance(c, dict) and c.get("name"):
                columns.add(str(c["name"]).lower())
    return tables, columns


def _looks_like_identifier(token: str) -> bool:
    return token.startswith(("dim_", "fact_", "stg_", "tbl_", "vw_")) or token.endswith(
        ("_key", "_id", "_mt", "_value", "_qty", "_flag", "_date", "_days")
    )


def check_rule(row: RuleRow, schema: dict[str, Any]) -> list[str]:
    """Warnings for table/column names in the rule that don't exist."""
    if row.kind == "vocabulary":
        return []
    tables, columns = _schema_names(schema)
    text = f"{row.rule} {row.guidance}"
    unknown: list[str] = []
    handled: set[str] = set()
    for left, right in _DOTTED_RE.findall(text):
        l_low, r_low = left.lower(), right.lower()
        if l_low in tables:
            if r_low not in columns and r_low not in tables:
                unknown.append(f"{left}.{right}")
        elif _looks_like_identifier(l_low):
            unknown.append(f"{left}.{right}")
        else:
            continue
        handled.update((l_low, r_low))
    for token in _SNAKE_RE.findall(text):
        # Settings keys ("default_sales_metric") are names, not references.
        if token in handled or token == row.name.lower() or token in tables or token in columns:
            continue
        if _looks_like_identifier(token):
            unknown.append(token)
    names: list[str] = []
    for name in unknown:
        if name.lower() not in {n.lower() for n in names}:
            names.append(name)
    if not names:
        return []
    return [f"mentions {', '.join(names[:5])}, which this connection doesn't have"]


# --- Storage -----------------------------------------------------------------------------


def load_rules(db: Session, tenant_id: UUID, connection_id: UUID) -> list[SemanticRule]:
    return list(
        db.scalars(
            select(SemanticRule)
            .where(
                SemanticRule.tenant_id == tenant_id,
                SemanticRule.connection_id == connection_id,
            )
            .order_by(SemanticRule.kind, SemanticRule.category, SemanticRule.rule_key)
        )
    )


def _terms(row: SemanticRule) -> list[str]:
    try:
        value = json.loads(row.terms_json or "[]")
    except json.JSONDecodeError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def rule_action(item: RuleRow, existing: dict[tuple[str, str], SemanticRule]) -> tuple[str, SemanticRule | None]:
    current = existing.get((item.kind, item.key))
    if current is None:
        return "create", None
    same = (
        current.rule == item.rule
        and current.guidance == item.guidance
        and current.category == item.category
        and _terms(current) == item.terms
    )
    return ("unchanged" if same else "update"), current


def existing_by_key(db: Session, tenant_id: UUID, connection_id: UUID) -> dict[tuple[str, str], SemanticRule]:
    return {(r.kind, r.rule_key): r for r in load_rules(db, tenant_id, connection_id)}


def save_rules(
    db: Session, *, tenant_id: UUID, connection_id: UUID, rows: list[RuleRow]
) -> dict[str, int]:
    """Upsert rules by (kind, key). Does not commit."""
    counts = {"created": 0, "updated": 0, "unchanged": 0, "rejected": 0}
    existing = existing_by_key(db, tenant_id, connection_id)
    for item in rows:
        if item.kind not in KINDS or not item.rule or (item.kind == "vocabulary" and not item.terms):
            counts["rejected"] += 1
            continue
        action, current = rule_action(item, existing)
        terms_json = json.dumps(item.terms) if item.terms else None
        if action == "unchanged":
            counts["unchanged"] += 1
        elif current is not None:
            current.name = item.name
            current.category = item.category
            current.rule = item.rule
            current.guidance = item.guidance
            current.terms_json = terms_json
            current.origin = item.origin
            counts["updated"] += 1
        else:
            row = SemanticRule(
                tenant_id=tenant_id,
                connection_id=connection_id,
                kind=item.kind,
                rule_key=item.key,
                name=item.name,
                category=item.category,
                rule=item.rule,
                guidance=item.guidance,
                terms_json=terms_json,
                origin=item.origin if item.origin in ("import", "ai") else "import",
            )
            db.add(row)
            existing[(item.kind, item.key)] = row
            counts["created"] += 1
    return counts


def rule_dict(row: SemanticRule) -> dict[str, Any]:
    return {
        "kind": row.kind,
        "name": row.name,
        "category": row.category,
        "rule": row.rule,
        "guidance": row.guidance,
        "terms": _terms(row),
        "origin": row.origin,
    }


# --- Feeding the agent --------------------------------------------------------------------


def _clip(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= _PROMPT_TEXT_CHARS else text[: _PROMPT_TEXT_CHARS - 1].rstrip() + "…"


def format_rules_for_prompt(rules: list[dict[str, Any]] | None) -> list[str]:
    """Prompt lines for the semantic section; empty when there are none."""
    if not rules:
        return []
    items = [r for r in rules if isinstance(r, dict) and r.get("rule")]
    lines: list[str] = []
    business = [r for r in items if r.get("kind") == "rule"][:_PROMPT_RULES]
    settings = [r for r in items if r.get("kind") == "setting"][:_PROMPT_RULES]
    vocabulary = [r for r in items if r.get("kind") == "vocabulary"][:_PROMPT_VOCABULARY]
    if business:
        lines.append("Business rules (follow these when writing SQL):")
        for r in business:
            prefix = f"[{r['category']}] " if r.get("category") else ""
            guard = f" ({_clip(r['guidance'])})" if r.get("guidance") else ""
            lines.append(f"  - {prefix}{_clip(r['rule'])}{guard}")
    if settings:
        lines.append("Defaults:")
        for r in settings:
            guard = f" — {_clip(r['guidance'])}" if r.get("guidance") else ""
            lines.append(f"  - {_clip(r['rule'])}{guard}")
    if vocabulary:
        lines.append("Business vocabulary (words users may use for each concept):")
        for r in vocabulary:
            terms = ", ".join(str(t) for t in (r.get("terms") or [])[:10])
            lines.append(f"  - {r['rule']}: {terms}")
    return lines


def expand_question(question: str, rules: list[dict[str, Any]] | None) -> str:
    """Append the concept for any vocabulary synonym found in the question,
    so "top vendors" also ranks tables named after "supplier"."""
    if not question or not rules:
        return question
    lowered = question.lower()
    extra: list[str] = []
    for r in rules:
        if not isinstance(r, dict) or r.get("kind") != "vocabulary":
            continue
        concept = str(r.get("rule") or "")
        if not concept or concept.lower() in lowered:
            continue
        for term in r.get("terms") or []:
            term_low = str(term).lower()
            if term_low and re.search(rf"(?<!\w){re.escape(term_low)}s?(?!\w)", lowered):
                extra.append(concept.replace("_", " "))
                break
    return f"{question} ({' '.join(extra)})" if extra else question
