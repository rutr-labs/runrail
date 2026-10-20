/* ─── Run forms ──────────────────────────────────────────────
   A workflow's typed parameters: what "Run with…" asks for, and what the
   editor builds. The server checks and converts every value again
   (runrail.run_parameters); this decides what the form shows and catches
   the obvious mistakes before a request is made. */

export type ParameterType = 'text' | 'number' | 'date' | 'choice' | 'bool';

export interface RunParameter {
  name: string;
  type: ParameterType;
  label?: string;
  help?: string;
  default?: string | number | boolean;
  choices?: string[];
  required?: boolean;
}

export const PARAMETER_TYPES: { type: ParameterType; label: string }[] = [
  { type: 'text', label: 'Text' },
  { type: 'number', label: 'Number' },
  { type: 'date', label: 'Date' },
  { type: 'choice', label: 'Choice' },
  { type: 'bool', label: 'Yes / no' },
];

export const RELATIVE_DATES: Record<string, number> = { today: 0, yesterday: -1 };
const RESERVED = new Set(['ds_utc', 'ts', 'ts_nodash', 'parameters', 'run_id', 'workflow_run_id',
  'task_run_id', 'project_root', 'artifacts_dir']);

/** What a form input holds: text for everything but yes/no. */
export type FormValue = string | boolean;
export type FormValues = Record<string, FormValue>;

/** Today's date on a workflow's calendar, as YYYY-MM-DD. */
export function localDate(zone: string | null | undefined, now = new Date(), offsetDays = 0): string {
  const shifted = new Date(now.getTime() + offsetDays * 86_400_000);
  try {
    return new Intl.DateTimeFormat('en-CA', { timeZone: zone || 'UTC', year: 'numeric', month: '2-digit', day: '2-digit' })
      .format(shifted);
  } catch {
    return shifted.toISOString().slice(0, 10);
  }
}

/** The value a field starts with: its default, a relative date as the date it means today. */
export function initialValue(parameter: RunParameter, zone?: string | null, now = new Date()): FormValue {
  const d = parameter.default;
  if (parameter.type === 'bool') return d === true;
  if (d === undefined || d === null) return '';
  if (parameter.type === 'date' && typeof d === 'string' && d in RELATIVE_DATES) {
    return localDate(zone, now, RELATIVE_DATES[d]);
  }
  return String(d);
}

export function initialValues(form: RunParameter[], zone?: string | null, now = new Date()): FormValues {
  return Object.fromEntries(form.map(p => [p.name, initialValue(p, zone, now)]));
}

export const labelOf = (p: RunParameter) => p.label || p.name;

/** "Default: yesterday" — said under a field whose default is not obvious from its value. */
export function defaultNote(parameter: RunParameter): string | null {
  const d = parameter.default;
  if (parameter.type === 'date' && typeof d === 'string' && d in RELATIVE_DATES) {
    return `Defaults to ${d} on the workflow's calendar`;
  }
  return null;
}

/** The parameters to send, or the first problem. An empty field is left out,
 *  so the server applies the default (and a relative date stays relative). */
export function collect(form: RunParameter[], values: FormValues): { parameters: Record<string, unknown> } | { error: string } {
  const parameters: Record<string, unknown> = {};
  for (const p of form) {
    const raw = values[p.name];
    if (p.type === 'bool') { parameters[p.name] = raw === true; continue; }
    const text = typeof raw === 'string' ? raw.trim() : '';
    if (!text) {
      if (p.required && p.default === undefined) return { error: `${labelOf(p)} is required` };
      continue;
    }
    if (p.type === 'number') {
      const n = Number(text);
      if (!Number.isFinite(n)) return { error: `${labelOf(p)}: "${text}" is not a number` };
      parameters[p.name] = n;
    } else if (p.type === 'date') {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return { error: `${labelOf(p)}: use YYYY-MM-DD` };
      parameters[p.name] = text;
    } else if (p.type === 'choice') {
      if (!p.choices?.includes(text)) return { error: `${labelOf(p)}: pick one of ${p.choices?.join(', ')}` };
      parameters[p.name] = text;
    } else {
      parameters[p.name] = raw;
    }
  }
  return { parameters };
}

/** Required parameters without a default: a run cannot start without being asked. */
export const needsAsking = (form: RunParameter[] | null | undefined) =>
  (form ?? []).filter(p => p.required && p.default === undefined).map(p => p.name);

/* ── The editor ─────────────────────────────────────────── */

/** One row of the editor, as typed: choices and the default are text until saved. */
export interface DraftParameter {
  key: number;
  name: string; type: ParameterType; label: string; help: string;
  defaultText: string; defaultBool: boolean; choicesText: string; required: boolean;
}

let nextKey = 1;
export function draftOf(p?: RunParameter): DraftParameter {
  return {
    key: nextKey++,
    name: p?.name ?? '', type: p?.type ?? 'text', label: p?.label ?? '', help: p?.help ?? '',
    defaultText: p && p.type !== 'bool' && p.default !== undefined ? String(p.default) : '',
    defaultBool: p?.type === 'bool' && p.default === true,
    choicesText: (p?.choices ?? []).join(', '),
    required: p?.required ?? false,
  };
}

export const parseChoices = (text: string) =>
  text.split(/[,\n]/).map(c => c.trim()).filter(Boolean);

/** Why a draft cannot be saved, or null. Same rules as the server. */
export function draftProblem(draft: DraftParameter, all: DraftParameter[]): string | null {
  const name = draft.name.trim();
  if (!name) return 'Give it a name';
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) return 'Letters, digits and underscores, not starting with a digit';
  if (RESERVED.has(name)) return `${name} is set by RunRail for every run`;
  if (all.filter(d => d.name.trim() === name).length > 1) return `${name} is in the form twice`;
  if (name === 'ds' && draft.type !== 'date') return 'ds is the run\'s date: make it a date';
  const text = draft.defaultText.trim();
  if (draft.type === 'choice') {
    const choices = parseChoices(draft.choicesText);
    if (choices.length === 0) return 'List at least one option';
    if (new Set(choices).size !== choices.length) return 'An option is listed twice';
    if (text && !choices.includes(text)) return `The default must be one of ${choices.join(', ')}`;
  }
  if (draft.type === 'number' && text && !Number.isFinite(Number(text))) return 'The default must be a number';
  if (draft.type === 'date' && text && !(text in RELATIVE_DATES) && !/^\d{4}-\d{2}-\d{2}$/.test(text)) {
    return 'The default is a date (YYYY-MM-DD), today or yesterday';
  }
  return null;
}

/** The drafts as the server stores them. */
export function toDefinitions(drafts: DraftParameter[]): RunParameter[] {
  return drafts.map(d => {
    const p: RunParameter = { name: d.name.trim(), type: d.type };
    if (d.label.trim()) p.label = d.label.trim();
    if (d.help.trim()) p.help = d.help.trim();
    if (d.type === 'choice') p.choices = parseChoices(d.choicesText);
    if (d.required) p.required = true;
    const text = d.defaultText.trim();
    if (d.type === 'bool') p.default = d.defaultBool;
    else if (text) p.default = d.type === 'number' ? Number(text) : text;
    return p;
  });
}
