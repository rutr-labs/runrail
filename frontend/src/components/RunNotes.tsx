import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, MessageSquare, StickyNote } from 'lucide-react';
import clsx from 'clsx';
import { api, del, post, put } from '../api';
import { rrws } from '../ws';
import { Button, EmptyState } from './ui';
import { useToast } from './toast';

/* ─── Run notes ────────────────────────────────────────────
   The annotation thread on a run: why it failed, what was checked, what to do
   if it happens again. Append-only in shape (a thread, not one editable
   field), because the second annotation on an incident must not destroy the
   first.

   Writes are optimistic but never dishonest. A note that has not landed yet
   is visibly pending; a note that failed to land STAYS on screen marked as
   unsaved, with the server's message and a retry — it is never quietly
   dropped, and it never sits there looking saved. Same for edits (rolled back
   on failure) and removals (dimmed while in flight, restored if the delete
   fails).

   There is no auth anywhere in RunRail, so `author` is voluntary attribution
   and is labelled as such everywhere it appears. Nothing here may read as a
   verified identity. */

const MAX_BODY = 4000;    // mirrors RunNoteIn.body max_length
const MAX_AUTHOR = 80;    // mirrors RunNoteIn.author max_length
const AUTHOR_KEY = 'runrail.notes.author';
/** Slop for "was this edited?": created_at and updated_at are written in the
 *  same transaction and can differ by microseconds. */
const EDIT_SLOP_MS = 1500;

/** GET /api/runs/{id}/notes — also embedded as `notes` on GET /api/runs/{id}. */
export interface RunNote {
  id: number;
  workflow_run_id: number;
  body: string;
  author: string | null;
  created_at: string;
  updated_at: string;
}

/* Mirrors formatDate/timeAgo in App.tsx, which does not export them.
   Lift both to a shared module when it does. */
const DAY = 86_400_000;

function formatDate(value?: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleString(undefined, {
    month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }),
    hour: '2-digit', minute: '2-digit',
  });
}

function timeAgo(value?: string | null): string {
  if (!value) return '—';
  const ms = Date.now() - new Date(value).getTime();
  if (ms < 60_000) return 'just now';
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  if (ms < DAY) return `${Math.floor(ms / 3_600_000)}h ago`;
  return `${Math.floor(ms / DAY)}d ago`;
}

const reason = (error: unknown) =>
  error instanceof Error && error.message ? error.message : 'the request failed';

const UNVERIFIED = 'Typed by whoever wrote the note. RunRail has no accounts, so this name is not verified.';

/** A creation that has not landed yet — or has failed and is waiting for the
 *  person to retry or discard it. Keyed separately from server notes so a
 *  failure can never masquerade as a saved row. */
interface Draft {
  key: string;
  body: string;
  author: string | null;
  sending: boolean;
  error?: string;
}

export interface RunNotesProps {
  runId: number | string;
  /** GET /api/runs/{id} already returns `notes`; pass them to paint instantly.
   *  Only used as the first frame — this component then owns the thread. */
  initialNotes?: RunNote[];
  /** Called after any successful write, for hosts that show a note count. */
  onChanged?: () => void;
  className?: string;
}

export function RunNotes({ runId, initialNotes, onChanged, className }: RunNotesProps) {
  const { toast } = useToast();
  const [notes, setNotes] = useState<RunNote[] | null>(initialNotes ?? null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [body, setBody] = useState('');
  const [author, setAuthor] = useState(rememberedAuthor);
  const [editing, setEditing] = useState<{ id: number; body: string; author: string } | null>(null);
  const [busy, setBusy] = useState<Record<number, 'saving' | 'deleting'>>({});
  const [rowError, setRowError] = useState<Record<number, string>>({});

  const draftSeq = useRef(0);
  /* Our own writes broadcast run_notes_changed too. Reloading on that echo
     would race the success path and could resurrect a row we just removed. */
  const mutating = useRef(0);
  const seed = useRef(initialNotes);
  seed.current = initialNotes;

  const reload = async () => {
    try {
      setNotes(await api<RunNote[]>(`/runs/${runId}/notes`));
      setLoadError(null);
    } catch (error) {
      setNotes(current => current ?? []);
      setLoadError(`Could not load notes — ${reason(error)}`);
    }
  };

  useEffect(() => {
    setNotes(seed.current ?? null);
    setDrafts([]);
    setEditing(null);
    setBusy({});
    setRowError({});
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  useEffect(() => rrws.on('run_notes_changed', event => {
    if (String(event.run_id) !== String(runId)) return;
    if (mutating.current > 0) return;
    void reload();
  }),
  // eslint-disable-next-line react-hooks/exhaustive-deps
  [runId]);

  /* ─── Create ───────────────────────────────────────── */
  const send = async (draft: Draft) => {
    setDrafts(list => list.map(d => (d.key === draft.key ? { ...d, sending: true, error: undefined } : d)));
    mutating.current++;
    try {
      const created = await post<RunNote>(`/runs/${runId}/notes`, { body: draft.body, author: draft.author });
      setNotes(current => [...(current ?? []), created]);
      setDrafts(list => list.filter(d => d.key !== draft.key));
      onChanged?.();
    } catch (error) {
      const message = reason(error);
      setDrafts(list => list.map(d => (d.key === draft.key ? { ...d, sending: false, error: message } : d)));
      toast(`Note not saved — ${message}`, 'error');
    } finally {
      mutating.current--;
    }
  };

  const submit = (event: { preventDefault: () => void }) => {
    event.preventDefault();
    const text = body.trim();
    if (!text) return;
    const draft: Draft = {
      key: `draft-${++draftSeq.current}`,
      body: text,
      author: author.trim() || null,
      sending: true,
    };
    setDrafts(list => [...list, draft]);
    setBody('');
    rememberAuthor(draft.author);
    void send(draft);
  };

  /* ─── Edit ─────────────────────────────────────────── */
  const saveEdit = async () => {
    if (!editing) return;
    const original = (notes ?? []).find(n => n.id === editing.id);
    const text = editing.body.trim();
    if (!original || !text) return;
    const optimistic: RunNote = { ...original, body: text, author: editing.author.trim() || null };
    setNotes(current => (current ?? []).map(n => (n.id === original.id ? optimistic : n)));
    setEditing(null);
    setBusy(state => ({ ...state, [original.id]: 'saving' }));
    setRowError(state => without(state, original.id));
    mutating.current++;
    try {
      const saved = await put<RunNote>(`/run-notes/${original.id}`,
        { body: optimistic.body, author: optimistic.author });
      setNotes(current => (current ?? []).map(n => (n.id === saved.id ? saved : n)));
      rememberAuthor(optimistic.author);
      onChanged?.();
    } catch (error) {
      const message = reason(error);
      // Put the note back exactly as the server still has it.
      setNotes(current => (current ?? []).map(n => (n.id === original.id ? original : n)));
      setRowError(state => ({ ...state, [original.id]: `Edit not saved — ${message}. The note is unchanged.` }));
      toast(`Edit not saved — ${message}`, 'error');
    } finally {
      mutating.current--;
      setBusy(state => without(state, original.id));
    }
  };

  /* ─── Delete ───────────────────────────────────────── */
  const remove = async (note: RunNote) => {
    if (!confirm('Remove this note? It cannot be recovered.')) return;
    setBusy(state => ({ ...state, [note.id]: 'deleting' }));
    setRowError(state => without(state, note.id));
    mutating.current++;
    try {
      await del(`/run-notes/${note.id}`);
      setNotes(current => (current ?? []).filter(n => n.id !== note.id));
      onChanged?.();
    } catch (error) {
      const message = reason(error);
      setRowError(state => ({ ...state, [note.id]: `Not removed — ${message}. The note is still there.` }));
      toast(`Note not removed — ${message}`, 'error');
    } finally {
      mutating.current--;
      setBusy(state => without(state, note.id));
    }
  };

  const rows = notes ?? [];
  const total = rows.length + drafts.length;

  return (
    <div className={clsx('panel', 'run-notes', className)}>
      <div className="panel-head">
        <div>
          <h2>Notes</h2>
          <p>What happened on this run, in your own words.</p>
        </div>
        {total > 0 && <span className="run-notes-count">{total} {total === 1 ? 'note' : 'notes'}</span>}
      </div>

      <p className="run-notes-disclaimer">
        Notes are visible to anyone who can reach this RunRail, and anyone can edit or remove them.
        There are no accounts, so a name on a note is self-reported.
      </p>

      {loadError && (
        <p className="run-notes-error run-notes-error--panel">
          <AlertTriangle size={12} /> {loadError}
          <button className="edit-link" onClick={() => void reload()}>Try again</button>
        </p>
      )}

      {total === 0 && !loadError ? (
        <EmptyState
          icon={<MessageSquare size={22} />}
          title="No notes on this run"
          text="Leave a note so the next person to open this run knows what you already found."
        />
      ) : (
        <div className="run-notes-thread">
          {rows.map(note => {
            const state = busy[note.id];
            const edited = new Date(note.updated_at).getTime() - new Date(note.created_at).getTime() > EDIT_SLOP_MS;
            const isEditing = editing?.id === note.id;
            return (
              <article key={note.id}
                       className={clsx('run-note', state === 'deleting' && 'run-note--leaving',
                                       rowError[note.id] && 'run-note--failed')}>
                <div className="run-note-head">
                  <span className={clsx('run-note-author', !note.author && 'run-note-author--unsigned')}
                        title={note.author ? UNVERIFIED : 'Nobody put a name on this note.'}>
                    {note.author || 'Unsigned'}
                  </span>
                  <span className="run-note-time" title={formatDate(note.created_at)}>
                    {timeAgo(note.created_at)}
                  </span>
                  {edited && (
                    <span className="run-note-edited" title={`Last edited ${formatDate(note.updated_at)}`}>edited</span>
                  )}
                  {state && <span className="run-note-state">{state === 'saving' ? 'Saving…' : 'Removing…'}</span>}
                  {!state && !isEditing && (
                    <div className="run-note-actions">
                      <button className="edit-link"
                              onClick={() => setEditing({ id: note.id, body: note.body, author: note.author ?? '' })}>
                        Edit
                      </button>
                      <button className="delete-link" onClick={() => void remove(note)}>Remove</button>
                    </div>
                  )}
                </div>

                {isEditing ? (
                  <div className="run-note-edit">
                    <textarea value={editing.body} maxLength={MAX_BODY} rows={3} autoFocus
                              onChange={e => setEditing({ ...editing, body: e.target.value })}
                              onKeyDown={e => {
                                if (e.key === 'Escape') { e.preventDefault(); setEditing(null); }
                                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void saveEdit(); }
                              }} />
                    <input value={editing.author} maxLength={MAX_AUTHOR} placeholder="Attribution (optional)"
                           aria-label="Attribution — free text, not a verified identity"
                           onChange={e => setEditing({ ...editing, author: e.target.value })} />
                    <div className="run-note-edit-actions">
                      <Button size="sm" onClick={() => void saveEdit()} disabled={!editing.body.trim()}>Save</Button>
                      <Button size="sm" variant="ghost" onClick={() => setEditing(null)}>Cancel</Button>
                    </div>
                  </div>
                ) : (
                  <p className="run-note-body">{note.body}</p>
                )}

                {rowError[note.id] && (
                  <p className="run-note-error"><AlertTriangle size={11} /> {rowError[note.id]}</p>
                )}
              </article>
            );
          })}

          {drafts.map(draft => (
            <article key={draft.key}
                     className={clsx('run-note', 'run-note--draft', draft.error && 'run-note--failed')}>
              <div className="run-note-head">
                <span className={clsx('run-note-author', !draft.author && 'run-note-author--unsigned')}>
                  {draft.author || 'Unsigned'}
                </span>
                <span className="run-note-state">{draft.sending ? 'Saving…' : 'Not saved'}</span>
                {!draft.sending && (
                  <div className="run-note-actions">
                    <button className="edit-link" onClick={() => void send(draft)}>Retry</button>
                    <button className="delete-link"
                            onClick={() => setDrafts(list => list.filter(d => d.key !== draft.key))}>
                      Discard
                    </button>
                  </div>
                )}
              </div>
              <p className="run-note-body">{draft.body}</p>
              {draft.error && (
                <p className="run-note-error">
                  <AlertTriangle size={11} /> Not saved — {draft.error}. Nothing was written; retry or copy the text somewhere safe.
                </p>
              )}
            </article>
          ))}
        </div>
      )}

      <form className="run-notes-form" onSubmit={submit}>
        <label className="field">
          <span>Add a note <em>Markdown is not rendered — plain text</em></span>
          <textarea value={body} rows={3} maxLength={MAX_BODY}
                    placeholder="What happened, what you checked, what to do next…"
                    onChange={e => setBody(e.target.value)}
                    onKeyDown={e => {
                      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submit(e);
                    }} />
        </label>
        <div className="run-notes-form-foot">
          <label className="field run-notes-author-field">
            <span>Attribution <em>Optional — free text, not a verified identity</em></span>
            <input value={author} maxLength={MAX_AUTHOR} placeholder="e.g. Priya"
                   onChange={e => setAuthor(e.target.value)} />
          </label>
          <span className="run-notes-counter">{body.length}/{MAX_BODY}</span>
          <Button type="submit" size="sm" disabled={!body.trim()}>Add note</Button>
        </div>
      </form>
    </div>
  );
}

/* ─── Run table indicator ──────────────────────────────────
   An annotated failure should be visible from the list, without opening it.
   Fed by GET /api/runs/notes/summary, which answers for every run in one
   query instead of a per-row lookup. */

export interface RunNoteSummaryEntry {
  count: number;
  /** NOTE: the server returns the OLDEST note on the run (it iterates
   *  created_at ascending and keeps the first), so this is worded as "first
   *  note" everywhere. Do not relabel it "latest" without changing the
   *  backend — a stale first line presented as the latest word on an incident
   *  is exactly the sort of lie this feature must not tell. */
  preview: string;
}

/** Keyed by run id as a string — JSON object keys. */
export type RunNotesSummary = Record<string, RunNoteSummaryEntry>;

export function useRunNotesSummary(workflowId?: number | string | null, limit = 500) {
  const [summary, setSummary] = useState<RunNotesSummary>({});
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let stale = false;
    const params = new URLSearchParams({ limit: String(limit) });
    if (workflowId) params.set('workflow_id', String(workflowId));
    api<RunNotesSummary>(`/runs/notes/summary?${params}`)
      .then(data => { if (!stale) setSummary(data ?? {}); })
      .catch(() => { /* the indicator is an extra; its absence must not break a run table */ });
    return () => { stale = true; };
  }, [workflowId, limit, nonce]);

  const reload = () => setNonce(n => n + 1);
  useEffect(() => rrws.on('run_notes_changed', reload), []);

  return { summary, entryFor: (runId: number | string) => summary[String(runId)], reload };
}

export interface RunNotesIndicatorProps {
  entry?: RunNoteSummaryEntry;
  className?: string;
}

/** Renders nothing when a run has no notes, so it can sit unconditionally in
 *  every row. Deliberately not a link: run rows are already anchors. */
export function RunNotesIndicator({ entry, className }: RunNotesIndicatorProps) {
  if (!entry || entry.count < 1) return null;
  const tip = `${entry.count} ${entry.count === 1 ? 'note' : 'notes'} on this run`
    + (entry.preview ? `\nFirst note: “${entry.preview}”` : '');
  return (
    <span className={clsx('run-note-flag', className)} title={tip} aria-label={tip}>
      <StickyNote size={11} strokeWidth={2.5} />
      {entry.count}
    </span>
  );
}

/* ─── Small helpers ────────────────────────────────────── */
function without<T>(state: Record<number, T>, key: number): Record<number, T> {
  const next = { ...state };
  delete next[key];
  return next;
}

function rememberedAuthor(): string {
  try {
    return localStorage.getItem(AUTHOR_KEY) ?? '';
  } catch {
    return ''; // localStorage unavailable (private mode, etc.)
  }
}

function rememberAuthor(value: string | null) {
  try {
    if (value) localStorage.setItem(AUTHOR_KEY, value);
    else localStorage.removeItem(AUTHOR_KEY);
  } catch { /* non-fatal */ }
}
