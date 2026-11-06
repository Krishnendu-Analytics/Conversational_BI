"""One import for a whole semantic model: file sheets, AI mapping, AI drafts.

``build_preview`` is what the "import / generate" screen runs:

1. Read the file (optional). Sheets whose headers no importer recognises
   are mapped to known kinds by the LLM (``sheet_mapping``).
2. Parse every kind: data dictionary, KPIs, relationships, rules /
   settings / vocabulary, example questions.
3. With ``generate_missing``, draft whatever the file (and the connection)
   doesn't already have (``semantic_generation``). With no file at all,
   everything is drafted.
4. Check every row against the connection and label it create / update /
   unchanged / invalid, exactly as for a customer's own file.

Nothing is saved here.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.services import data_dictionary, semantic_examples, semantic_rules
from app.services import semantic_model_import as smi
from app.services.file_import import SourceDocument, load_source
from app.services.semantic_generation import generate_model
from app.services.sheet_mapping import map_unrecognised_sheets


@dataclass
class DictionaryPreview:
    row: data_dictionary.DictRow
    action: str  # create | update | unchanged
    existing: str | None = None


@dataclass
class RulePreview:
    row: semantic_rules.RuleRow
    action: str
    warnings: list[str]


@dataclass
class ExamplePreview:
    row: semantic_examples.ExampleRow
    action: str  # create | unchanged
    warnings: list[str]


@dataclass
class FlowPreview:
    kpis: list[smi.KpiPreview] = field(default_factory=list)
    relationships: list[smi.RelationshipPreview] = field(default_factory=list)
    rules: list[RulePreview] = field(default_factory=list)
    dictionary: list[DictionaryPreview] = field(default_factory=list)
    examples: list[ExamplePreview] = field(default_factory=list)
    skipped: list[tuple[int | None, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _parse_file(source: SourceDocument, preview: FlowPreview, schema: dict[str, Any]):
    try:
        model = smi.parse_semantic_model(source)
    except smi.SemanticModelError:
        model = smi.ParsedModel()
    examples, example_skips, _ = semantic_examples.parse_example_sheets(source.sheets)
    try:
        parsed_dict = data_dictionary.parse_dictionary_source(source)
        matched = data_dictionary.match_to_schema(parsed_dict.rows, schema)
        dict_rows = matched.rows
        preview.skipped += parsed_dict.skipped + matched.skipped
    except data_dictionary.DataDictionaryError:
        dict_rows = []
    preview.skipped += model.skipped + example_skips
    return model, examples, dict_rows


def build_preview(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID,
    schema: dict[str, Any],
    filename: str | None,
    content: bytes | None,
    generate_missing: bool,
    llm: Any,
    validator: smi.SqlValidator | None = None,
) -> FlowPreview:
    preview = FlowPreview()
    model = smi.ParsedModel()
    examples: list[semantic_examples.ExampleRow] = []
    dict_rows: list[data_dictionary.DictRow] = []

    if content is not None:
        source = load_source(filename or "upload", content)
        mapping = map_unrecognised_sheets(source, llm)
        preview.notes += [
            f"{n.sheet}: {n.message}" if n.sheet else n.message for n in mapping.notes
        ]
        model, examples, dict_rows = _parse_file(mapping.source, preview, schema)

    existing_rules = semantic_rules.existing_by_key(db, tenant_id, connection_id)
    existing_kinds = {kind for kind, _ in existing_rules}
    has_examples = bool(semantic_examples.existing_keys(db, tenant_id, connection_id))

    if generate_missing or content is None:
        file_rule_kinds = {r.kind for r in model.rules}
        wanted: set[str] = {"descriptions"}
        if not model.kpis:
            wanted.add("kpis")
        if not model.relationships:
            wanted.add("relationships")
        if not ({"rule", "setting"} & (file_rule_kinds | existing_kinds)):
            wanted.add("rules")
        if "vocabulary" not in file_rule_kinds | existing_kinds:
            wanted.add("vocabulary")
        if not examples and not has_examples:
            wanted.add("examples")
        # Tables the file already describes don't need an AI summary.
        described = copy.deepcopy(schema)
        data_dictionary.apply_to_schema(described, [_as_entry(r) for r in dict_rows])
        drafted = generate_model(described, wanted, llm)
        model.kpis += drafted.kpis
        model.relationships += drafted.relationships
        model.rules += drafted.rules
        examples += drafted.examples
        dict_rows = drafted.descriptions + dict_rows
        preview.notes += drafted.notes
        made = [
            label
            for label, items in (
                ("KPIs", drafted.kpis),
                ("joins", drafted.relationships),
                ("rules and vocabulary", drafted.rules),
                ("example questions", drafted.examples),
                ("table descriptions", drafted.descriptions),
            )
            if items
        ]
        if made:
            preview.notes.insert(0, "Drafted by AI (check before importing): " + ", ".join(made))

    if not (model.kpis or model.relationships or model.rules or examples or dict_rows):
        raise smi.SemanticModelError(
            "Nothing to import: no KPI, relationship, rule, vocabulary, example-question or "
            "data dictionary sheet was found, and AI could not draft anything."
        )

    preview.kpis = smi.preview_kpis(
        db, tenant_id=tenant_id, connection_id=connection_id,
        kpis=model.kpis, schema=schema, validator=validator,
    )
    preview.relationships = smi.preview_relationships(
        db, tenant_id=tenant_id, connection_id=connection_id,
        relationships=model.relationships, schema=schema,
    )
    preview.rules = [
        RulePreview(r, semantic_rules.rule_action(r, existing_rules)[0], semantic_rules.check_rule(r, schema))
        for r in model.rules
    ]
    example_keys = semantic_examples.existing_keys(db, tenant_id, connection_id)
    preview.examples = [
        ExamplePreview(
            e,
            "unchanged" if e.key in example_keys else "create",
            semantic_examples.example_warnings(e, schema),
        )
        for e in examples
    ]
    stored = {
        data_dictionary.entry_key(e): e
        for e in data_dictionary.load_entries(db, tenant_id, connection_id)
    }
    for row in dict_rows:
        current = stored.get(row.key)
        if current is None:
            preview.dictionary.append(DictionaryPreview(row, "create"))
        elif current.description == row.description:
            preview.dictionary.append(DictionaryPreview(row, "unchanged"))
        else:
            preview.dictionary.append(DictionaryPreview(row, "update", current.description))
    return preview


class _Entry:
    """Minimal stand-in for DataDictionaryEntry, for ``apply_to_schema``."""

    def __init__(self, row: data_dictionary.DictRow) -> None:
        self.schema_name = row.schema_name
        self.table_name = row.table_name
        self.column_name = row.column_name
        self.description = row.description


def _as_entry(row: data_dictionary.DictRow) -> Any:
    return _Entry(row)
