import { useState } from 'react';
import { ArrowDown, ArrowUp, ListChecks, Plus, Trash2 } from 'lucide-react';
import clsx from 'clsx';
import {
  PARAMETER_TYPES, defaultNote, draftOf, draftProblem, labelOf, localDate, toDefinitions,
  type DraftParameter, type FormValues, type ParameterType, type RunParameter,
} from '../runForms';

/* ─── Run form ─────────────────────────────────────────────
   The editor (in "Edit workflow") and the fields ("Run with…", Backfill).

   The editor publishes through a hidden input, like ScheduleBuilder and
   WatchdogFields, so the host's FormData submit is untouched; runFormValue
   reads it back, or the first problem so the host can refuse to save. */

const FIELD = 'run_parameters';

export function runFormValue(f: FormData): { run_parameters_json: RunParameter[] | null } | { error: string } {
  const raw = String(f.get(FIELD) ?? '');
  if (!raw) return { run_parameters_json: null };
  const parsed = JSON.parse(raw) as { error?: string; form?: RunParameter[] };
  if (parsed.error) return { error: parsed.error };
  return { run_parameters_json: parsed.form && parsed.form.length ? parsed.form : null };
}

export function RunFormEditor({ initial }: { initial?: RunParameter[] | null }) {
  const [drafts, setDrafts] = useState<DraftParameter[]>(() => (initial ?? []).map(p => draftOf(p)));
  const problems = drafts.map(d => draftProblem(d, drafts));
  const first = problems.findIndex(Boolean);
  const published = JSON.stringify(first >= 0
    ? { error: `Run form, ${drafts[first].name.trim() || `parameter ${first + 1}`}: ${problems[first]}` }
    : { form: toDefinitions(drafts) });
  const change = (key: number, patch: Partial<DraftParameter>) =>
    setDrafts(all => all.map(d => d.key === key ? { ...d, ...patch } : d));
  const move = (index: number, by: number) => setDrafts(all => {
    const next = [...all];
    const [row] = next.splice(index, 1);
    next.splice(index + by, 0, row);
    return next;
  });
  return (
    <fieldset className="runform-editor">
      <legend><ListChecks size={13} /> Run form <em>Optional</em></legend>
      <p className="runform-intro">
        The values a run asks for. <b>Run with…</b> shows them as a form; scheduled runs use the defaults.
        Tasks read them as <code>{'{{ name }}'}</code>, notebooks as parameters, scripts as <code>RUNRAIL_PARAM_NAME</code>.
      </p>
      <input type="hidden" name={FIELD} value={published} />
      {drafts.map((d, i) => (
        <div key={d.key} className={clsx('runform-row', problems[i] && 'has-problem')}>
          <div className="runform-row-main">
            <label className="field"><span>Name</span>
              <input value={d.name} placeholder="report_date" spellCheck={false} autoComplete="off"
                     onChange={e => change(d.key, { name: e.target.value })} />
            </label>
            <label className="field"><span>Type</span>
              <select value={d.type} onChange={e => change(d.key, { type: e.target.value as ParameterType })}>
                {PARAMETER_TYPES.map(t => <option key={t.type} value={t.type}>{t.label}</option>)}
              </select>
            </label>
            {d.type === 'bool' ? (
              <label className="field runform-bool"><span>Default</span>
                <select value={d.defaultBool ? 'yes' : 'no'} onChange={e => change(d.key, { defaultBool: e.target.value === 'yes' })}>
                  <option value="no">No</option><option value="yes">Yes</option>
                </select>
              </label>
            ) : d.type === 'date' ? (
              <label className="field"><span>Default <em>Optional</em></span>
                <input list="runform-relative-dates" value={d.defaultText} placeholder="yesterday or 2026-01-31"
                       onChange={e => change(d.key, { defaultText: e.target.value })} />
              </label>
            ) : (
              <label className="field"><span>Default <em>Optional</em></span>
                <input value={d.defaultText} inputMode={d.type === 'number' ? 'decimal' : undefined}
                       placeholder={d.type === 'choice' ? 'One of the options' : ''}
                       onChange={e => change(d.key, { defaultText: e.target.value })} />
              </label>
            )}
            <div className="runform-row-actions">
              <button type="button" className="icon-button" disabled={i === 0} onClick={() => move(i, -1)} aria-label="Move up" title="Move up"><ArrowUp size={13} /></button>
              <button type="button" className="icon-button" disabled={i === drafts.length - 1} onClick={() => move(i, 1)} aria-label="Move down" title="Move down"><ArrowDown size={13} /></button>
              <button type="button" className="icon-button is-danger" onClick={() => setDrafts(all => all.filter(x => x.key !== d.key))}
                      aria-label={`Remove ${d.name || 'parameter'}`} title="Remove"><Trash2 size={13} /></button>
            </div>
          </div>
          {d.type === 'choice' && (
            <label className="field"><span>Options <em>comma-separated</em></span>
              <input value={d.choicesText} placeholder="EU, US, APAC" onChange={e => change(d.key, { choicesText: e.target.value })} />
            </label>
          )}
          <div className="runform-row-extra">
            <label className="field"><span>Label <em>Optional</em></span>
              <input value={d.label} placeholder="Report date" onChange={e => change(d.key, { label: e.target.value })} />
            </label>
            <label className="field"><span>Help <em>Optional</em></span>
              <input value={d.help} placeholder="Shown under the field" onChange={e => change(d.key, { help: e.target.value })} />
            </label>
            {d.type !== 'bool' && (
              <label className="runform-required">
                <input type="checkbox" checked={d.required} onChange={e => change(d.key, { required: e.target.checked })} />
                Required
              </label>
            )}
          </div>
          {problems[i] && <small className="runform-problem" role="alert">{problems[i]}</small>}
        </div>
      ))}
      <datalist id="runform-relative-dates"><option value="today" /><option value="yesterday" /></datalist>
      <button type="button" className="btn btn-ghost btn-sm runform-add" onClick={() => setDrafts(all => [...all, draftOf()])}>
        <Plus size={13} /> Add a parameter
      </button>
    </fieldset>
  );
}

/** The form a run is started with. `blankDefaults` leaves fields empty with
 *  the default as a hint — for a backfill, where "yesterday" means the day
 *  before each backfilled date, not before today. */
export function RunFormFields({ form, values, onChange, zone, blankDefaults = false }: {
  form: RunParameter[]; values: FormValues; onChange: (next: FormValues) => void;
  zone?: string | null; blankDefaults?: boolean;
}) {
  const set = (name: string, value: string | boolean) => onChange({ ...values, [name]: value });
  return (
    <div className="runform-fields">
      {form.map(p => {
        const id = `run-param-${p.name}`;
        const value = values[p.name];
        const hint = p.help ?? (blankDefaults && p.default !== undefined ? `Default: ${String(p.default)}` : defaultNote(p));
        const required = p.required && p.default === undefined;
        if (p.type === 'bool') {
          return (
            <label key={p.name} className="field toggle-field runform-toggle" htmlFor={id}>
              <span>{labelOf(p)}{p.help && <small>{p.help}</small>}</span>
              <label className="toggle"><input id={id} type="checkbox" checked={value === true} onChange={e => set(p.name, e.target.checked)} /><span /></label>
            </label>
          );
        }
        const text = typeof value === 'string' ? value : '';
        return (
          <label key={p.name} className="field" htmlFor={id}>
            <span>{labelOf(p)}{required ? <em className="runform-req"> required</em> : null}{p.label && <code className="runform-name">{p.name}</code>}</span>
            {p.type === 'choice' ? (
              <select id={id} value={text} required={required} onChange={e => set(p.name, e.target.value)}>
                {(!required || !text) && <option value="">{blankDefaults && p.default !== undefined ? `Default (${String(p.default)})` : required ? 'Choose…' : '—'}</option>}
                {p.choices?.map(c => <option key={c} value={c}>{c}</option>)}
              </select>
            ) : p.type === 'date' ? (
              <div className="runform-date">
                <input id={id} type="date" value={text} required={required} onChange={e => set(p.name, e.target.value)} />
                {!blankDefaults && (
                  <span className="runform-quick">
                    <button type="button" onClick={() => set(p.name, localDate(zone, new Date(), 0))}>Today</button>
                    <button type="button" onClick={() => set(p.name, localDate(zone, new Date(), -1))}>Yesterday</button>
                  </span>
                )}
              </div>
            ) : (
              <input id={id} type={p.type === 'number' ? 'number' : 'text'} step="any" value={text} required={required}
                     placeholder={blankDefaults && p.default !== undefined ? String(p.default) : ''}
                     onChange={e => set(p.name, e.target.value)} />
            )}
            {hint && <small>{hint}</small>}
          </label>
        );
      })}
    </div>
  );
}
