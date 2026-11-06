import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type ReactNode,
} from "react";
import {
  ApiError,
  deleteDataDictionaryEntry,
  deleteSemanticExample,
  deleteSemanticRule,
  evaluateSemanticExamples,
  listDataDictionary,
  listSemanticExamples,
  listSemanticRules,
  previewSemanticModelImport,
  saveSemanticModelImport,
  updateDataDictionaryEntry,
  type DataDictionaryEntry,
  type ExampleEvaluation,
  type SemanticExample,
  type SemanticModelPreview,
  type SemanticModelRule,
  type SemanticRule,
  type SemanticRuleKind,
} from "@/lib/api";

const ACCEPT = ".csv,.tsv,.txt,.xlsx,.xlsm";
const SKIPPED_SHOWN = 15;
const DESCRIPTIONS_SHOWN = 200;

const KPI_ACTION_LABEL = {
  create: "New",
  update: "Changes existing",
  link: "Adds name to existing",
  unchanged: "Already saved",
  conflict: "Kept (confirmed)",
  invalid: "Can't import",
} as const;

const REL_ACTION_LABEL = {
  create: "New",
  confirm: "Confirms suggestion",
  unchanged: "Already known",
  invalid: "Can't import",
} as const;

const SIMPLE_ACTION_LABEL = {
  create: "New",
  update: "Changes existing",
  unchanged: "Already saved",
} as const;

const EVAL_LABEL = {
  pass: "Pass",
  wrong_tables: "Wrong tables",
  fallback: "Guessed",
  error: "Query failed",
  no_answer: "No answer",
} as const;

const KIND_LABEL: Record<SemanticRuleKind, string> = {
  rule: "Business rules",
  setting: "Defaults",
  vocabulary: "Vocabulary",
};

const KINDS: SemanticRuleKind[] = ["rule", "setting", "vocabulary"];

type Section = "kpis" | "relationships" | "rules" | "dictionary" | "examples";
const SECTIONS: Section[] = ["kpis", "relationships", "rules", "dictionary", "examples"];
type Selection = Record<Section, Set<number>>;

function emptySelection(): Selection {
  return {
    kpis: new Set(),
    relationships: new Set(),
    rules: new Set(),
    dictionary: new Set(),
    examples: new Set(),
  };
}

function errorText(err: unknown, fallback: string) {
  return err instanceof ApiError ? err.message : fallback;
}

function selectable(action: string) {
  return action !== "invalid" && action !== "unchanged" && action !== "conflict";
}

function plural(n: number, word: string) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function ruleText(r: SemanticModelRule) {
  if (r.kind === "vocabulary") return `${r.rule}: ${r.terms.join(", ")}`;
  return r.category ? `[${r.category}] ${r.rule}` : r.rule;
}

function descriptionLabel(d: DataDictionaryEntry) {
  return d.column_name ? `${d.table_name}.${d.column_name}` : d.table_name;
}

function Origin({ origin }: { origin: string }) {
  if (origin === "ai") return <span className="dict-origin">AI draft</span>;
  if (origin === "inferred") return <span className="dict-origin">Inferred</span>;
  return null;
}

function Issues({ error, warnings }: { error?: string | null; warnings?: string[] }) {
  return (
    <>
      {error ? <div className="model-issue">{error}</div> : null}
      {(warnings ?? []).map((w) => (
        <div key={w} className="model-warning">
          {w}
        </div>
      ))}
    </>
  );
}

/**
 * One place to build a connection's semantic model: import a CSV / Excel
 * file of any layout (unfamiliar sheets are read by AI) and let AI draft
 * whatever the file lacks — or draft everything from the schema when there
 * is no file. Every row is checked against the connection before saving.
 */
export function SemanticModelImportPanel({
  connectionId,
  canEdit,
  onImported,
}: {
  connectionId: string;
  canEdit: boolean;
  onImported?: () => void | Promise<void>;
}) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState<"file" | "ai" | null>(null);
  const [fillMissing, setFillMissing] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [preview, setPreview] = useState<SemanticModelPreview | null>(null);
  const [selected, setSelected] = useState<Selection>(emptySelection);
  const [overwriteDictionary, setOverwriteDictionary] = useState(false);
  const [rules, setRules] = useState<SemanticRule[]>([]);
  const [examples, setExamples] = useState<SemanticExample[]>([]);
  const [descriptions, setDescriptions] = useState<DataDictionaryEntry[]>([]);
  const [descFilter, setDescFilter] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [actingId, setActingId] = useState<string | null>(null);
  const [evaluating, setEvaluating] = useState(false);
  const [evaluation, setEvaluation] = useState<ExampleEvaluation | null>(null);

  const refreshSaved = useCallback(async () => {
    if (!connectionId) {
      setRules([]);
      setExamples([]);
      setDescriptions([]);
      return;
    }
    const [r, e, d] = await Promise.all([
      listSemanticRules(connectionId),
      listSemanticExamples(connectionId),
      listDataDictionary(connectionId),
    ]);
    setRules(r);
    setExamples(e);
    setDescriptions(d);
  }, [connectionId]);

  useEffect(() => {
    setPreview(null);
    setError(null);
    setMessage(null);
    setEvaluation(null);
    setDescFilter("");
    setEditingId(null);
    refreshSaved().catch((err) => setError(errorText(err, "Could not load the semantic model")));
  }, [refreshSaved]);

  async function runPreview(file: File | null) {
    setBusy(file ? "file" : "ai");
    setError(null);
    setMessage(null);
    try {
      const result = await previewSemanticModelImport(connectionId, file, fillMissing);
      const pick = (rows: { action: string }[]) =>
        new Set(rows.flatMap((row, i) => (selectable(row.action) ? [i] : [])));
      setPreview(result);
      setOverwriteDictionary(false);
      setSelected({
        kpis: pick(result.kpis),
        relationships: pick(result.relationships),
        rules: pick(result.rules),
        dictionary: pick(result.dictionary),
        examples: pick(result.examples),
      });
    } catch (err) {
      setError(errorText(err, file ? "Could not read that file" : "AI could not draft a model"));
    } finally {
      setBusy(null);
    }
  }

  async function handleFile(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (file) await runPreview(file);
  }

  function toggle(section: Section, i: number) {
    setSelected((prev) => {
      const next = new Set(prev[section]);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return { ...prev, [section]: next };
    });
  }

  function toggleAll(section: Section, rows: { action: string }[], on: boolean) {
    setSelected((prev) => ({
      ...prev,
      [section]: new Set(on ? rows.flatMap((r, i) => (selectable(r.action) ? [i] : [])) : []),
    }));
  }

  async function handleImport() {
    if (!preview) return;
    const pick = <T,>(section: Section, rows: T[]) => rows.filter((_, i) => selected[section].has(i));
    const payload = {
      kpis: pick("kpis", preview.kpis).map(({ name, formula, description, domain, synonyms }) => ({
        name,
        formula,
        description,
        domain,
        synonyms,
      })),
      relationships: pick("relationships", preview.relationships).map((r) => ({
        from_schema: r.from_schema,
        from_table: r.from_table,
        from_column: r.from_column,
        to_schema: r.to_schema,
        to_table: r.to_table,
        to_column: r.to_column,
        cardinality: r.cardinality,
        description: r.description,
      })),
      rules: pick("rules", preview.rules).map(
        ({ kind, name, category, rule, guidance, terms, origin }) => ({
          kind,
          name,
          category,
          rule,
          guidance,
          terms,
          origin,
        }),
      ),
      dictionary: pick("dictionary", preview.dictionary).map(
        ({ schema_name, table_name, column_name, description, origin }) => ({
          schema_name,
          table_name,
          column_name,
          description,
          origin,
        }),
      ),
      examples: pick("examples", preview.examples).map(
        ({ question, tables, domain, notes, origin }) => ({
          question,
          tables,
          domain,
          notes,
          origin,
        }),
      ),
      overwrite_dictionary: overwriteDictionary,
    };
    setSaving(true);
    try {
      const r = await saveSemanticModelImport(connectionId, payload);
      setPreview(null);
      const parts: string[] = [];
      const add = (n: number | undefined, text: string) => {
        if (n) parts.push(text.replace("#", String(n)));
      };
      add(r.kpis.created, "# KPIs added as suggested metrics");
      add(r.kpis.updated, "# metrics updated");
      add(r.kpis.linked, "# KPI names added to existing metrics");
      add((r.relationships.created ?? 0) + (r.relationships.confirmed ?? 0), "# joins saved");
      add((r.rules.created ?? 0) + (r.rules.updated ?? 0), "# rules saved");
      add((r.dictionary.created ?? 0) + (r.dictionary.updated ?? 0), "# descriptions saved");
      add(r.examples.created, "# example questions saved");
      const rejected = SECTIONS.reduce((sum, s) => sum + (r[s].rejected ?? 0), 0);
      add(rejected, "# no longer match the schema");
      setError(null);
      setMessage(
        `Import complete: ${parts.join(", ") || "nothing changed"}.` +
          (r.kpis.created || r.kpis.updated ? " Confirm the new metrics above so chat can use them." : ""),
      );
      await Promise.all([onImported?.(), refreshSaved()]);
    } catch (err) {
      setError(errorText(err, "Import failed"));
    } finally {
      setSaving(false);
    }
  }

  async function handleDeleteRule(rule: SemanticRule) {
    if (!window.confirm(`Delete "${ruleText(rule)}"?`)) return;
    setActingId(rule.id);
    try {
      await deleteSemanticRule(rule.id);
      await refreshSaved();
    } catch (err) {
      setError(errorText(err, "Could not delete rule"));
    } finally {
      setActingId(null);
    }
  }

  async function handleDeleteExample(example: SemanticExample) {
    if (!window.confirm(`Delete the question "${example.question}"?`)) return;
    setActingId(example.id);
    try {
      await deleteSemanticExample(example.id);
      await refreshSaved();
    } catch (err) {
      setError(errorText(err, "Could not delete question"));
    } finally {
      setActingId(null);
    }
  }

  async function handleSaveDescription(entry: DataDictionaryEntry) {
    const text = editText.trim();
    if (!text || text === entry.description) {
      setEditingId(null);
      return;
    }
    setActingId(entry.id);
    try {
      await updateDataDictionaryEntry(entry.id, text);
      setEditingId(null);
      await refreshSaved();
    } catch (err) {
      setError(errorText(err, "Could not save description"));
    } finally {
      setActingId(null);
    }
  }

  async function handleDeleteDescription(entry: DataDictionaryEntry) {
    if (!window.confirm(`Delete the description of ${descriptionLabel(entry)}?`)) return;
    setActingId(entry.id);
    try {
      await deleteDataDictionaryEntry(entry.id);
      await refreshSaved();
    } catch (err) {
      setError(errorText(err, "Could not delete description"));
    } finally {
      setActingId(null);
    }
  }

  async function handleEvaluate() {
    setEvaluating(true);
    setError(null);
    try {
      setEvaluation(await evaluateSemanticExamples(connectionId));
    } catch (err) {
      setError(errorText(err, "Evaluation failed"));
    } finally {
      setEvaluating(false);
    }
  }

  const filteredDescriptions = useMemo(() => {
    const q = descFilter.trim().toLowerCase();
    const sorted = [...descriptions].sort(
      (a, b) =>
        a.table_name.localeCompare(b.table_name) || a.column_name.localeCompare(b.column_name),
    );
    if (!q) return sorted;
    return sorted.filter(
      (d) =>
        descriptionLabel(d).toLowerCase().includes(q) ||
        d.description.toLowerCase().includes(q),
    );
  }, [descriptions, descFilter]);
  const describedTables = new Set(descriptions.map((d) => `${d.schema_name}.${d.table_name}`))
    .size;

  function sectionTable(
    section: Section,
    title: string,
    rows: { action: string }[],
    headers: string[],
    renderRow: (index: number) => ReactNode[],
    { collapsed = false }: { collapsed?: boolean } = {},
  ) {
    if (rows.length === 0) return null;
    const chosen = selected[section].size;
    const available = rows.filter((r) => selectable(r.action)).length;
    const table = (
      <div className="dict-table-wrap">
        <table className="dict-table">
          <thead>
            <tr>
              <th>
                <input
                  type="checkbox"
                  checked={available > 0 && chosen === available}
                  disabled={available === 0}
                  onChange={(e) => toggleAll(section, rows, e.target.checked)}
                  aria-label={`Include all ${title}`}
                />
              </th>
              {headers.map((h) => (
                <th key={h}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i} className={selected[section].has(i) ? "" : "is-excluded"}>
                <td>
                  <input
                    type="checkbox"
                    checked={selected[section].has(i)}
                    disabled={!selectable(row.action)}
                    onChange={() => toggle(section, i)}
                    aria-label={`Include ${title} row ${i + 1}`}
                  />
                </td>
                {renderRow(i).map((cell, j) => (
                  <td key={j} className={j === 0 ? "dict-term-cell" : undefined}>
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
    return (
      <details className="model-section" open={!collapsed}>
        <summary>
          <strong>{title}</strong>
          <span className="form-hint">
            {chosen} of {rows.length} selected
          </span>
        </summary>
        {table}
      </details>
    );
  }

  const selectedCount = SECTIONS.reduce((sum, s) => sum + selected[s].size, 0);
  const dictionaryUpdates = preview
    ? preview.dictionary.filter((row, i) => selected.dictionary.has(i) && row.action === "update")
        .length
    : 0;
  const passRate =
    evaluation && evaluation.total ? Math.round((100 * evaluation.passed) / evaluation.total) : 0;

  return (
    <section className="profile-card dict-panel">
      <div className="dict-header">
        <div>
          <h2>Semantic model</h2>
          <p className="form-hint">
            KPIs, joins, business rules, vocabulary, descriptions and example questions. Import
            them from a CSV or Excel file in any layout, or let AI draft them from this
            connection's tables. Everything is checked against the connection before it is
            saved.
          </p>
        </div>
        {canEdit ? (
          <div className="dict-header-actions dict-import-actions">
            <label className="dict-overwrite">
              <input
                type="checkbox"
                checked={fillMissing}
                onChange={(e) => setFillMissing(e.target.checked)}
                disabled={busy !== null}
              />
              Let AI fill in anything the file is missing
            </label>
            <input
              ref={fileInput}
              type="file"
              accept={ACCEPT}
              hidden
              onChange={(e) => void handleFile(e)}
            />
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => fileInput.current?.click()}
              disabled={busy !== null || saving || !connectionId}
            >
              {busy === "file" ? "Checking file…" : "Import CSV / Excel"}
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void runPreview(null)}
              disabled={busy !== null || saving || !connectionId}
            >
              {busy === "ai" ? "AI is drafting…" : "Generate with AI"}
            </button>
          </div>
        ) : null}
      </div>

      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
      {message ? (
        <p className="form-success" role="status">
          {message}
        </p>
      ) : null}

      {preview ? (
        <div className="dict-preview">
          <div className="dict-preview-head">
            <strong>{preview.filename}</strong>
            <span className="form-hint">
              Untick anything you don't want. Rows marked "AI draft" or "Inferred" were not in
              your file — check them before importing.
            </span>
          </div>
          {preview.notes.length > 0 ? (
            <ul className="dict-ai-note model-notes">
              {preview.notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          ) : null}

          {sectionTable("kpis", "KPIs", preview.kpis, ["KPI", "Formula", "Status"], (i) => {
            const k = preview.kpis[i];
            return [
              <>
                {k.name} <Origin origin={k.origin} />
                {k.domain ? <div className="dict-existing">{k.domain}</div> : null}
              </>,
              <>
                <code className="model-formula">{k.expression || k.formula}</code>
                <Issues error={k.error} warnings={k.warnings} />
                {k.existing_name &&
                (k.action === "update" || k.action === "conflict" || k.action === "link") ? (
                  <div className="dict-existing">
                    Existing “{k.existing_name}”: {k.existing_expression}
                  </div>
                ) : null}
              </>,
              <span className={`dict-action dict-action-${k.action}`}>
                {KPI_ACTION_LABEL[k.action]}
              </span>,
            ];
          })}

          {sectionTable(
            "relationships",
            "Joins",
            preview.relationships,
            ["Join", "Status"],
            (i) => {
              const r = preview.relationships[i];
              return [
                <>
                  <code className="model-formula">
                    {r.join || `${r.from_table}.${r.from_column} → ${r.to_table}.${r.to_column}`}
                  </code>{" "}
                  <Origin origin={r.origin} />
                  <Issues error={r.error} warnings={r.warnings} />
                  {r.note ? <div className="dict-existing">{r.note}</div> : null}
                </>,
                <span className={`dict-action dict-action-${r.action}`}>
                  {REL_ACTION_LABEL[r.action]}
                </span>,
              ];
            },
          )}

          {sectionTable(
            "rules",
            "Rules, defaults & vocabulary",
            preview.rules,
            ["Type", "Rule", "Status"],
            (i) => {
              const r = preview.rules[i];
              return [
                <>
                  {KIND_LABEL[r.kind]} <Origin origin={r.origin} />
                  {r.name && r.kind === "rule" ? (
                    <div className="dict-existing">{r.name}</div>
                  ) : null}
                </>,
                <>
                  {ruleText(r)}
                  {r.guidance ? <div className="dict-existing">{r.guidance}</div> : null}
                  <Issues warnings={r.warnings} />
                </>,
                <span className={`dict-action dict-action-${r.action}`}>
                  {SIMPLE_ACTION_LABEL[r.action]}
                </span>,
              ];
            },
          )}

          {sectionTable(
            "examples",
            "Example questions",
            preview.examples,
            ["Question", "Tables", "Status"],
            (i) => {
              const e = preview.examples[i];
              return [
                <>
                  {e.question} <Origin origin={e.origin} />
                  {e.domain ? <div className="dict-existing">{e.domain}</div> : null}
                </>,
                <>
                  {e.tables.join(", ")}
                  <Issues warnings={e.warnings} />
                </>,
                <span className={`dict-action dict-action-${e.action}`}>
                  {SIMPLE_ACTION_LABEL[e.action]}
                </span>,
              ];
            },
          )}

          {sectionTable(
            "dictionary",
            "Descriptions (data dictionary)",
            preview.dictionary,
            ["Table", "Column", "Description", "Status"],
            (i) => {
              const d = preview.dictionary[i];
              return [
                d.table_name,
                d.column_name || <span className="dict-origin">Table summary</span>,
                <>
                  {d.description} <Origin origin={d.origin} />
                  {d.action === "update" && d.existing_description ? (
                    <div className="dict-existing">Current: {d.existing_description}</div>
                  ) : null}
                </>,
                <span className={`dict-action dict-action-${d.action}`}>
                  {SIMPLE_ACTION_LABEL[d.action]}
                </span>,
              ];
            },
            { collapsed: true },
          )}

          {preview.skipped.length > 0 ? (
            <details className="dict-skipped">
              <summary>{plural(preview.skipped.length, "skipped row")}</summary>
              <ul>
                {preview.skipped.slice(0, SKIPPED_SHOWN).map((s, i) => (
                  <li key={`${s.row ?? "x"}-${i}`}>
                    {s.row != null ? `Row ${s.row}: ` : ""}
                    {s.reason}
                  </li>
                ))}
                {preview.skipped.length > SKIPPED_SHOWN ? (
                  <li>…and {preview.skipped.length - SKIPPED_SHOWN} more</li>
                ) : null}
              </ul>
            </details>
          ) : null}

          <div className="dict-preview-actions">
            {dictionaryUpdates > 0 ? (
              <label className="dict-overwrite">
                <input
                  type="checkbox"
                  checked={overwriteDictionary}
                  onChange={(e) => setOverwriteDictionary(e.target.checked)}
                />
                Replace {plural(dictionaryUpdates, "existing description")}
              </label>
            ) : (
              <span className="form-hint">
                KPIs are added as suggested metrics; everything else is used right away.
              </span>
            )}
            <div className="dict-preview-buttons">
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => setPreview(null)}
                disabled={saving}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void handleImport()}
                disabled={saving || selectedCount === 0}
              >
                {saving ? "Importing…" : `Import ${selectedCount} selected`}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {rules.length > 0 || examples.length > 0 || descriptions.length > 0 ? (
        <div className="dict-groups">
          {KINDS.map((kind) => {
            const items = rules.filter((r) => r.kind === kind);
            if (items.length === 0) return null;
            return (
              <details key={kind} className="dict-group">
                <summary>
                  <strong>{KIND_LABEL[kind]}</strong>
                  <span className="dict-group-summary">
                    Used by the assistant when it writes queries
                  </span>
                  <span className="dict-group-count">{items.length}</span>
                </summary>
                <ul className="dict-list">
                  {items.map((r) => (
                    <li key={r.id} className="dict-entry">
                      <span className="dict-entry-name">{r.name || KIND_LABEL[kind]}</span>
                      <span className="dict-definition">
                        {ruleText(r)} <Origin origin={r.origin} />
                        {r.guidance ? <div className="dict-existing">{r.guidance}</div> : null}
                      </span>
                      {canEdit ? (
                        <span className="semantic-actions">
                          <button
                            type="button"
                            className="btn btn-small btn-danger-ghost"
                            onClick={() => void handleDeleteRule(r)}
                            disabled={actingId === r.id}
                          >
                            Delete
                          </button>
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </details>
            );
          })}

          {descriptions.length > 0 ? (
            <details className="dict-group">
              <summary>
                <strong>Descriptions</strong>
                <span className="dict-group-summary">
                  What each table and column means, across {plural(describedTables, "table")}
                </span>
                <span className="dict-group-count">{descriptions.length}</span>
              </summary>
              <div className="dict-toolbar">
                <input
                  className="dict-filter"
                  type="search"
                  value={descFilter}
                  onChange={(e) => setDescFilter(e.target.value)}
                  placeholder="Search tables, columns or descriptions…"
                  aria-label="Search descriptions"
                />
                {filteredDescriptions.length > DESCRIPTIONS_SHOWN ? (
                  <span className="form-hint">
                    Showing {DESCRIPTIONS_SHOWN} of {filteredDescriptions.length}. Search to
                    narrow it down.
                  </span>
                ) : null}
              </div>
              <ul className="dict-list">
                {filteredDescriptions.slice(0, DESCRIPTIONS_SHOWN).map((d) => {
                  const editing = editingId === d.id;
                  const label = descriptionLabel(d);
                  return (
                    <li key={d.id} className={`dict-entry${editing ? " is-editing" : ""}`}>
                      <span className="dict-entry-name">
                        {d.column_name ? label : `${label} (table)`}
                      </span>
                      {editing ? (
                        <textarea
                          value={editText}
                          onChange={(e) => setEditText(e.target.value)}
                          rows={2}
                          aria-label={`Description of ${label}`}
                        />
                      ) : (
                        <span className="dict-definition">
                          {d.description} <Origin origin={d.origin} />
                        </span>
                      )}
                      {canEdit ? (
                        <span className="semantic-actions">
                          {editing ? (
                            <>
                              <button
                                type="button"
                                className="btn btn-small btn-primary"
                                onClick={() => void handleSaveDescription(d)}
                                disabled={actingId === d.id}
                              >
                                {actingId === d.id ? "…" : "Save"}
                              </button>
                              <button
                                type="button"
                                className="btn btn-small btn-ghost"
                                onClick={() => setEditingId(null)}
                              >
                                Cancel
                              </button>
                            </>
                          ) : (
                            <>
                              <button
                                type="button"
                                className="btn btn-small btn-ghost"
                                onClick={() => {
                                  setEditingId(d.id);
                                  setEditText(d.description);
                                }}
                                disabled={actingId === d.id}
                              >
                                Edit
                              </button>
                              <button
                                type="button"
                                className="btn btn-small btn-danger-ghost"
                                onClick={() => void handleDeleteDescription(d)}
                                disabled={actingId === d.id}
                              >
                                Delete
                              </button>
                            </>
                          )}
                        </span>
                      ) : null}
                    </li>
                  );
                })}
                {filteredDescriptions.length === 0 ? (
                  <li className="form-hint">Nothing matches "{descFilter}".</li>
                ) : null}
              </ul>
            </details>
          ) : null}

          {examples.length > 0 ? (
            <details className="dict-group" open={Boolean(evaluation)}>
              <summary>
                <strong>Example questions</strong>
                <span className="dict-group-summary">
                  {evaluation
                    ? `Last check: ${evaluation.passed} of ${evaluation.total} answered correctly (${passRate}%)`
                    : "Test the assistant against these"}
                </span>
                <span className="dict-group-count">{examples.length}</span>
              </summary>
              {canEdit ? (
                <div className="model-eval-actions">
                  <button
                    type="button"
                    className="btn btn-small btn-primary"
                    onClick={() => void handleEvaluate()}
                    disabled={evaluating}
                  >
                    {evaluating ? "Asking the assistant…" : "Run evaluation"}
                  </button>
                  <span className="form-hint">
                    Asks the assistant each question (up to 20), runs its query and checks it
                    used the expected tables.
                  </span>
                </div>
              ) : null}
              {evaluation ? (
                <div className="dict-table-wrap">
                  <table className="dict-table">
                    <thead>
                      <tr>
                        <th>Question</th>
                        <th>Result</th>
                        <th>Details</th>
                      </tr>
                    </thead>
                    <tbody>
                      {evaluation.results.map((r) => (
                        <tr key={r.question}>
                          <td>{r.question}</td>
                          <td>
                            <span
                              className={`dict-action ${
                                r.status === "pass" ? "dict-action-create" : "dict-action-invalid"
                              }`}
                            >
                              {EVAL_LABEL[r.status]}
                            </span>
                          </td>
                          <td>
                            {r.tables_used.length ? (
                              <div>Used: {r.tables_used.join(", ")}</div>
                            ) : null}
                            {r.missing_tables.length ? (
                              <div className="model-warning">
                                Missing: {r.missing_tables.join(", ")}
                              </div>
                            ) : null}
                            {r.error ? <div className="model-issue">{r.error}</div> : null}
                            {r.row_count != null ? (
                              <div className="dict-existing">{plural(r.row_count, "row")}</div>
                            ) : null}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <ul className="dict-list">
                  {examples.map((e) => (
                    <li key={e.id} className="dict-entry">
                      <span className="dict-entry-name">{e.domain || "Question"}</span>
                      <span className="dict-definition">
                        {e.question} <Origin origin={e.origin} />
                        {e.tables.length ? (
                          <div className="dict-existing">{e.tables.join(", ")}</div>
                        ) : null}
                      </span>
                      {canEdit ? (
                        <span className="semantic-actions">
                          <button
                            type="button"
                            className="btn btn-small btn-danger-ghost"
                            onClick={() => void handleDeleteExample(e)}
                            disabled={actingId === e.id}
                          >
                            Delete
                          </button>
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
            </details>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
