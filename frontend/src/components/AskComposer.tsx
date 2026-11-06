import { useRef, useState, type FormEvent, type KeyboardEvent } from "react";

export type ComposerAttachment = {
  id: string;
  file: File;
};

/** Something the user can point the question at by typing ``@``. */
export type MentionOption = {
  kind: "auto" | "database" | "document";
  id: string;
  label: string;
  detail?: string;
};

/** The source(s) the chat is currently pointed at, shown as chips. */
export type ScopeChip = {
  kind: "database" | "document";
  id: string;
  label: string;
};

type Props = {
  onSubmit: (question: string, attachments: File[]) => void;
  disabled?: boolean;
  busy?: boolean;
  placeholder?: string;
  mentionOptions?: MentionOption[];
  onMention?: (option: MentionOption) => void;
  scopeChips?: ScopeChip[];
  onRemoveScopeChip?: (chip: ScopeChip) => void;
};

const MENTION_GROUPS: { kind: MentionOption["kind"]; title: string }[] = [
  { kind: "auto", title: "Sources" },
  { kind: "database", title: "Databases" },
  { kind: "document", title: "Files" },
];

/** ``@query`` right before the caret, if the user is typing a mention. */
function mentionAtCaret(text: string, caret: number): { start: number; query: string } | null {
  const match = /(^|\s)@([^\s@]*)$/.exec(text.slice(0, caret));
  if (!match) return null;
  return { start: caret - match[2].length - 1, query: match[2] };
}

function ScopeIcon({ kind }: { kind: MentionOption["kind"] }) {
  if (kind === "database") {
    return (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden>
        <ellipse cx="12" cy="5.5" rx="7.5" ry="3" stroke="currentColor" strokeWidth="1.9" />
        <path
          d="M4.5 5.5v13c0 1.66 3.36 3 7.5 3s7.5-1.34 7.5-3v-13M4.5 12c0 1.66 3.36 3 7.5 3s7.5-1.34 7.5-3"
          stroke="currentColor"
          strokeWidth="1.9"
        />
      </svg>
    );
  }
  if (kind === "document") {
    return (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden>
        <path
          d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Z"
          stroke="currentColor"
          strokeWidth="1.9"
          strokeLinejoin="round"
        />
        <path d="M14 2v6h6" stroke="currentColor" strokeWidth="1.9" strokeLinejoin="round" />
      </svg>
    );
  }
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="m12 3 2.2 5.8L20 11l-5.8 2.2L12 19l-2.2-5.8L4 11l5.8-2.2L12 3Z"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinejoin="round"
      />
    </svg>
  );
}

const ACCEPTED =
  ".pdf,.docx,.doc,.xlsx,.xlsm,.xls,.csv,.tsv,.txt,.md,.json,.png,.jpg,.jpeg,.webp";

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** ChatGPT/Claude-style composer with document attach + send. */
export function AskComposer({
  onSubmit,
  disabled = false,
  busy = false,
  placeholder = "Ask a question about your data…",
  mentionOptions = [],
  onMention,
  scopeChips = [],
  onRemoveScopeChip,
}: Props) {
  const [question, setQuestion] = useState("");
  const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
  const [mention, setMention] = useState<{ start: number; query: string } | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const fileRef = useRef<HTMLInputElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const locked = disabled || busy;
  const mentionsEnabled = mentionOptions.length > 0 && Boolean(onMention);

  const query = mention?.query.toLowerCase() ?? "";
  const matches = mention
    ? mentionOptions.filter(
        (o) =>
          o.label.toLowerCase().includes(query) || (o.detail ?? "").toLowerCase().includes(query),
      )
    : [];
  // Keyboard order matches the grouped on-screen order.
  const ordered = MENTION_GROUPS.flatMap((g) => matches.filter((o) => o.kind === g.kind));
  const menuOpen = mention !== null && ordered.length > 0;

  function syncMention(text: string, caret: number) {
    if (!mentionsEnabled) return;
    const next = mentionAtCaret(text, caret);
    if (next?.query !== mention?.query) setActiveIndex(0);
    setMention(next);
  }

  function pickMention(option: MentionOption) {
    if (!mention) return;
    const el = inputRef.current;
    const caret = el?.selectionStart ?? question.length;
    // Drop the typed "@query"; the choice shows as a chip instead.
    setQuestion(question.slice(0, mention.start) + question.slice(caret));
    const at = mention.start;
    setMention(null);
    onMention?.(option);
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(at, at);
    });
  }

  function openMentionPicker() {
    const el = inputRef.current;
    const caret = el?.selectionStart ?? question.length;
    const before = question.slice(0, caret);
    const insert = before === "" || /\s$/.test(before) ? "@" : " @";
    const pos = caret + insert.length;
    setQuestion(before + insert + question.slice(caret));
    setMention({ start: pos - 1, query: "" });
    setActiveIndex(0);
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(pos, pos);
    });
  }

  function addFiles(list: FileList | null) {
    if (!list?.length) return;
    const next = Array.from(list).map((file) => ({
      // randomUUID is missing on plain-http pages (not a secure context).
      id:
        typeof crypto.randomUUID === "function"
          ? crypto.randomUUID()
          : Date.now().toString(36) + Math.random().toString(36).slice(2),
      file,
    }));
    setAttachments((prev) => [...prev, ...next].slice(0, 8));
  }

  function removeAttachment(id: string) {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }

  function handleSubmit(e?: FormEvent) {
    e?.preventDefault();
    const trimmed = question.trim();
    if ((!trimmed && attachments.length === 0) || locked) return;
    onSubmit(
      trimmed || "Analyze the attached documents.",
      attachments.map((a) => a.file),
    );
    setQuestion("");
    setAttachments([]);
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (menuOpen) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const step = e.key === "ArrowDown" ? 1 : -1;
        setActiveIndex((i) => (i + step + ordered.length) % ordered.length);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        pickMention(ordered[Math.min(activeIndex, ordered.length - 1)]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setMention(null);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  }

  const canSend = !locked && (question.trim().length > 0 || attachments.length > 0);

  return (
    <form className="composer" onSubmit={handleSubmit}>
      {attachments.length > 0 ? (
        <ul className="composer-attachments" aria-label="Attached files">
          {attachments.map(({ id, file }) => (
            <li key={id} className="composer-attachment">
              <span className="composer-attachment-icon" aria-hidden>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
                  <path
                    d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Z"
                    stroke="currentColor"
                    strokeWidth="1.75"
                    strokeLinejoin="round"
                  />
                  <path
                    d="M14 2v6h6"
                    stroke="currentColor"
                    strokeWidth="1.75"
                    strokeLinejoin="round"
                  />
                </svg>
              </span>
              <span className="composer-attachment-meta">
                <span className="composer-attachment-name">{file.name}</span>
                <span className="composer-attachment-size">{formatSize(file.size)}</span>
              </span>
              <button
                type="button"
                className="composer-attachment-remove"
                onClick={() => removeAttachment(id)}
                aria-label={`Remove ${file.name}`}
                disabled={locked}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      <div className="composer-shell">
        {menuOpen ? (
          <div className="mention-menu" role="listbox" id="composer-mentions" aria-label="Ask about">
            {MENTION_GROUPS.map((group) => {
              const items = ordered.filter((o) => o.kind === group.kind);
              if (items.length === 0) return null;
              return (
                <div key={group.kind} className="mention-group" role="presentation">
                  <p className="mention-group-title">{group.title}</p>
                  {items.map((option) => {
                    const index = ordered.indexOf(option);
                    return (
                      <button
                        key={`${option.kind}:${option.id}`}
                        type="button"
                        role="option"
                        id={`mention-${index}`}
                        aria-selected={index === activeIndex}
                        className={`mention-option${index === activeIndex ? " is-active" : ""}`}
                        onMouseDown={(e) => e.preventDefault()}
                        onMouseEnter={() => setActiveIndex(index)}
                        onClick={() => pickMention(option)}
                      >
                        <span className={`mention-icon mention-icon-${option.kind}`}>
                          <ScopeIcon kind={option.kind} />
                        </span>
                        <span className="mention-label">{option.label}</span>
                        {option.detail ? (
                          <span className="mention-detail">{option.detail}</span>
                        ) : null}
                      </button>
                    );
                  })}
                </div>
              );
            })}
          </div>
        ) : null}

        {scopeChips.length > 0 ? (
          <div className="scope-chips" aria-label="Answering from">
            <span className="scope-chips-label">Asking</span>
            {scopeChips.map((chip) => (
              <span key={`${chip.kind}:${chip.id}`} className={`scope-chip scope-chip-${chip.kind}`}>
                <ScopeIcon kind={chip.kind} />
                <span className="scope-chip-name" title={chip.label}>
                  {chip.label}
                </span>
                {onRemoveScopeChip ? (
                  <button
                    type="button"
                    className="scope-chip-remove"
                    onClick={() => onRemoveScopeChip(chip)}
                    disabled={locked}
                    aria-label={`Stop asking only ${chip.label}`}
                    title="Stop asking only this source"
                  >
                    ×
                  </button>
                ) : null}
              </span>
            ))}
          </div>
        ) : null}

        <textarea
          ref={inputRef}
          className="composer-input"
          rows={1}
          value={question}
          onChange={(e) => {
            setQuestion(e.target.value);
            syncMention(e.target.value, e.target.selectionStart ?? e.target.value.length);
            const el = e.target;
            el.style.height = "auto";
            el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
          }}
          onSelect={(e) => syncMention(e.currentTarget.value, e.currentTarget.selectionStart ?? 0)}
          onBlur={() => setMention(null)}
          placeholder={placeholder}
          disabled={locked}
          onKeyDown={onKeyDown}
          aria-label="Your question"
          role={mentionsEnabled ? "combobox" : undefined}
          aria-expanded={mentionsEnabled ? menuOpen : undefined}
          aria-controls={menuOpen ? "composer-mentions" : undefined}
          aria-activedescendant={menuOpen ? `mention-${activeIndex}` : undefined}
          aria-autocomplete={mentionsEnabled ? "list" : undefined}
        />

        <div className="composer-toolbar">
          <input
            ref={fileRef}
            type="file"
            className="visually-hidden"
            accept={ACCEPTED}
            multiple
            onChange={(e) => {
              addFiles(e.target.files);
              e.target.value = "";
            }}
          />
          <button
            type="button"
            className="composer-icon-btn"
            onClick={() => fileRef.current?.click()}
            disabled={locked}
            aria-label="Attach documents"
            title="Attach documents"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
              <path
                d="M21.44 11.05 12.25 20.24a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"
                stroke="currentColor"
                strokeWidth="1.75"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
          {mentionsEnabled ? (
            <button
              type="button"
              className="composer-icon-btn composer-mention-btn"
              onClick={openMentionPicker}
              disabled={locked}
              aria-label="Choose a database or file to ask"
              title="Ask a specific database or file (@)"
            >
              @
            </button>
          ) : null}

          <button
            type="submit"
            className="composer-send"
            disabled={!canSend}
            aria-label={busy ? "Thinking" : "Send"}
          >
            {busy ? (
              <span className="composer-send-busy" aria-hidden />
            ) : (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
                <path
                  d="M5 12h14M13 6l6 6-6 6"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            )}
          </button>
        </div>
      </div>

      <p className="composer-hint">
        Enter to send · Shift+Enter for new line ·{" "}
        {mentionsEnabled ? "@ to pick a database or file" : "PDF, Word, Excel, CSV, images"}
      </p>
    </form>
  );
}
