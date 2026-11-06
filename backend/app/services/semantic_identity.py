"""Universal semantic identity: physical-source names, synonyms, catalog aliases."""

from __future__ import annotations

import json
import re
from typing import Any

_NAME_MAX = 120


def humanize_identifier(raw: str) -> str:
    cleaned = re.sub(r"[_\-\.]+", " ", (raw or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.title() if cleaned else ""


def unique_name(base: str, used: set[str], *, max_len: int = _NAME_MAX) -> str:
    """Return a case-insensitive-unique display name, clipping to the column width."""
    base = (base or "Field").strip() or "Field"
    if len(base) > max_len:
        base = base[:max_len].rstrip()
    candidate = base
    suffix = 2
    while candidate.lower() in used:
        suffix_text = f" {suffix}"
        trimmed = base[: max(1, max_len - len(suffix_text))].rstrip()
        candidate = f"{trimmed}{suffix_text}"
        suffix += 1
    used.add(candidate.lower())
    return candidate


def field_identity_name(col_name: str, *, table_label: str, used: set[str]) -> str:
    """Stable per-column identity. Qualify with table label only on collision."""
    base = humanize_identifier(col_name) or "Field"
    if base.lower() in used:
        table = humanize_identifier(table_label)
        qualified = f"{table} {base}".strip() if table else base
        return unique_name(qualified or base, used)
    return unique_name(base, used)


def encode_synonyms(*labels: str | None, name: str) -> str | None:
    seen: set[str] = set()
    out: list[str] = []
    name_key = (name or "").strip().lower()
    for label in labels:
        token = (label or "").strip()
        if not token:
            continue
        key = token.lower()
        if key == name_key or key in seen:
            continue
        seen.add(key)
        out.append(token)
    if not out:
        return None
    return json.dumps(out)


def parse_synonyms(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [text]
        values = parsed if isinstance(parsed, list) else [text]
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        token = str(item).strip()
        key = token.lower()
        if not token or key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


def catalog_item_aliases(item: dict[str, Any]) -> list[str]:
    name = str(item.get("name") or "").strip()
    aliases: list[str] = []
    seen: set[str] = set()
    for token in (name, str(item.get("label") or "").strip(), *parse_synonyms(
        item.get("synonyms") if item.get("synonyms") is not None else item.get("synonyms_json")
    )):
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        aliases.append(token)
    return aliases


def canonical_name_map(items: list[Any] | None) -> dict[str, str]:
    """Lowercase alias → canonical name. Ambiguous synonyms are omitted."""
    by_name: dict[str, str] = {}
    synonym_targets: dict[str, set[str]] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        by_name[name.lower()] = name
        for alias in catalog_item_aliases(item):
            alias_key = alias.lower()
            if alias_key == name.lower():
                continue
            synonym_targets.setdefault(alias_key, set()).add(name)
    out = dict(by_name)
    for alias_key, names in synonym_targets.items():
        if alias_key in out:
            continue
        if len(names) == 1:
            out[alias_key] = next(iter(names))
    return out


def expression_map_for_items(items: list[Any] | None) -> dict[str, str]:
    """Map lowercase alias → expression.

    Canonical names always win. Generic synonyms are added only when they
    resolve to exactly one catalog item, so ambiguous labels never silently
    pick the wrong physical column.
    """
    by_name: dict[str, str] = {}
    synonym_targets: dict[str, set[str]] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        expr = str(item.get("expression") or "").strip()
        if not name or not expr:
            continue
        name_key = name.lower()
        by_name[name_key] = expr
        for alias in catalog_item_aliases(item):
            alias_key = alias.lower()
            if alias_key == name_key:
                continue
            synonym_targets.setdefault(alias_key, set()).add(name_key)
    out = dict(by_name)
    for alias_key, names in synonym_targets.items():
        if alias_key in out:
            continue
        if len(names) == 1:
            out[alias_key] = by_name[next(iter(names))]
    return out


def ambiguous_alias_targets(items: list[Any] | None) -> dict[str, list[str]]:
    """Lowercase alias → canonical names when more than one item shares the alias."""
    targets: dict[str, list[str]] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        for alias in catalog_item_aliases(item):
            key = alias.lower()
            names = targets.setdefault(key, [])
            if name not in names:
                names.append(name)
    return {key: names for key, names in targets.items() if len(names) > 1}


def _normalize_match_text(value: str) -> str:
    """Normalize natural-language and identifier forms for matching.

    Treat separators such as underscores, hyphens, dots, and whitespace
    as equivalent so names like ``loan_amount`` and ``loan amount``
    resolve to the same semantic identity.
    """
    text = (value or "").strip().lower()
    text = re.sub(r"[_\-.]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def match_catalog_names(question: str, items: list[Any] | None) -> list[str]:
    """Canonical names whose name, label, or synonym appears in the question."""
    text = _normalize_match_text(question)

    matched: list[str] = []
    seen: set[str] = set()

    for item in items or []:
        if not isinstance(item, dict):
            continue

        name = str(item.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue

        for alias in catalog_item_aliases(item):
            normalized_alias = _normalize_match_text(alias)

            if normalized_alias and normalized_alias in text:
                matched.append(name)
                seen.add(name.lower())
                break

    return matched