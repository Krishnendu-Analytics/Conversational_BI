import { useEffect, useState } from "react";
import {
  ApiError,
  clearMemories,
  deleteMemory,
  getMemories,
  setMemoryEnabled,
  type MemoryItem,
} from "@/lib/api";

/** What the analyst remembers across chats: view, delete, or switch off. */
export function MemorySettings() {
  const [enabled, setEnabled] = useState(true);
  const [items, setItems] = useState<MemoryItem[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getMemories()
      .then((res) => {
        if (cancelled) return;
        setEnabled(res.enabled);
        setItems(res.items);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Could not load memory");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not update memory");
    } finally {
      setBusy(false);
    }
  }

  const facts = (items ?? []).filter((m) => m.kind === "fact");
  const chats = (items ?? []).filter((m) => m.kind === "conversation");

  return (
    <section className="profile-card memory-card">
      <div className="memory-head">
        <div>
          <h2>Memory</h2>
          <p className="form-hint">
            The analyst remembers your preferences and earlier chats, and uses them for context
            in new conversations. Only you can see this.
          </p>
        </div>
        <label className="memory-toggle">
          <input
            type="checkbox"
            checked={enabled}
            disabled={busy || items === null}
            onChange={(e) =>
              void run(async () => {
                const res = await setMemoryEnabled(e.target.checked);
                setEnabled(res.enabled);
              })
            }
          />
          <span>{enabled ? "On" : "Off"}</span>
        </label>
      </div>

      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}

      {items === null && !error ? <p className="loading-copy">Loading memory…</p> : null}

      {items !== null ? (
        <>
          <h3 className="memory-subhead">About you</h3>
          {facts.length === 0 ? (
            <p className="form-hint">Nothing yet. Facts appear as you chat.</p>
          ) : (
            <ul className="memory-list">
              {facts.map((m) => (
                <MemoryRow key={m.id} item={m} busy={busy} onDelete={(id) => void run(() => remove(id))} />
              ))}
            </ul>
          )}

          <h3 className="memory-subhead">Past chats</h3>
          {chats.length === 0 ? (
            <p className="form-hint">No chat summaries yet.</p>
          ) : (
            <ul className="memory-list">
              {chats.map((m) => (
                <MemoryRow key={m.id} item={m} busy={busy} onDelete={(id) => void run(() => remove(id))} />
              ))}
            </ul>
          )}

          {items.length > 0 ? (
            <div className="form-actions">
              <button
                type="button"
                className="btn btn-ghost"
                disabled={busy}
                onClick={() => {
                  if (!window.confirm("Forget everything the analyst remembers about you?")) return;
                  void run(async () => {
                    await clearMemories();
                    setItems([]);
                  });
                }}
              >
                Clear all memory
              </button>
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  );

  async function remove(id: string) {
    await deleteMemory(id);
    setItems((prev) => (prev ?? []).filter((m) => m.id !== id));
  }
}

function MemoryRow({
  item,
  busy,
  onDelete,
}: {
  item: MemoryItem;
  busy: boolean;
  onDelete: (id: string) => void;
}) {
  return (
    <li className="memory-item">
      <div className="memory-text">
        {item.conversation_title ? <strong>{item.conversation_title}</strong> : null}
        <span>{item.content}</span>
      </div>
      <button
        type="button"
        className="chat-doc-remove"
        onClick={() => onDelete(item.id)}
        disabled={busy}
        aria-label="Forget this"
        title="Forget this"
      >
        ×
      </button>
    </li>
  );
}
