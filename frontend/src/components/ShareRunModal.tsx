import { useEffect, useRef, useState } from 'react';
import { Download, Info, ShieldAlert } from 'lucide-react';
import clsx from 'clsx';
import { api } from '../api';
import { Button, CancelButton, LoadingBar, Modal } from './ui';
import type { RunOutputs } from './ReportPanel';

/* ─── Budget constants ────────────────────────────────────
   Kept in step with src/runrail/reports.py. */

/** Gmail's attachment limit — the number the export is designed against, and
 *  the cap the /export route enforces on max_bytes. */
const EXPORT_MAX_BYTES = 25 * 1024 * 1024;
/** Where most corporate mail relays start bouncing attachments. */
const WARN_BYTES = 10 * 1024 * 1024;

/** Runs whose snapshot would be a lie by the time it is read; /export answers
 *  409 for these rather than freeze a moving target. */
const IN_PROGRESS = new Set(['queued', 'running', 'waiting_approval']);

type LogMode = 'tail' | 'full' | 'none';

const LOG_MODES: { value: LogMode; label: string; hint: string }[] = [
  { value: 'tail', label: 'Tail',  hint: '≈128 KB / stream' },
  { value: 'full', label: 'Full',  hint: 'up to 2 MB total' },
  { value: 'none', label: 'None',  hint: 'no output' },
];

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

/** Split the API's three estimates back into the parts they were built from.
 *
 *  run_outputs() reports shell+logs+report, shell+logs and shell+report; the
 *  fourth combination the form allows (no logs, no report) is not in the
 *  payload, and these three simultaneous equations recover it exactly. */
function decompose(estimate: RunOutputs['estimated_export_bytes']) {
  const logs = Math.max(0, estimate.with_report - estimate.logs_none);
  const report = Math.max(0, estimate.with_report - estimate.without_report);
  const shell = Math.max(0, estimate.logs_none + estimate.without_report - estimate.with_report);
  return { logs, report, shell };
}

export interface ShareRunModalProps {
  runId: number | string;
  /** Run status from the host page; falls back to the outputs payload. */
  runStatus?: string;
  workflowName?: string | null;
  onClose: () => void;
}

export function ShareRunModal({ runId, runStatus, workflowName, onClose }: ShareRunModalProps) {
  const [outputs, setOutputs] = useState<RunOutputs | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [logs, setLogs] = useState<LogMode>('tail');
  const [includeReport, setIncludeReport] = useState(true);
  const [started, setStarted] = useState(false);
  const timer = useRef<number>(0);

  useEffect(() => {
    let cancelled = false;
    api<RunOutputs>(`/runs/${runId}/outputs`)
      .then(data => {
        if (cancelled) return;
        setOutputs(data);
        setIncludeReport(data.reports.length > 0);
      })
      .catch(problem => {
        if (!cancelled) setError(problem instanceof Error ? problem.message : 'Could not read this run.');
      });
    return () => { cancelled = true; };
  }, [runId]);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const status = runStatus ?? outputs?.status;
  const live = status != null && IN_PROGRESS.has(status);
  const hasReport = (outputs?.reports.length ?? 0) > 0;
  const reportOn = includeReport && hasReport;

  const parts = outputs ? decompose(outputs.estimated_export_bytes) : null;
  const estimate = parts
    ? parts.shell + (logs === 'none' ? 0 : parts.logs) + (reportOn ? parts.report : 0)
    : 0;
  const tone = estimate > EXPORT_MAX_BYTES ? 'over' : estimate > WARN_BYTES ? 'warn' : 'ok';
  const unrendered = Boolean(outputs?.reports.some(entry => !entry.rendered));

  // max_bytes is pinned to the route's ceiling rather than left at its 20 MB
  // default: the size readout already warns at 10 MB and 25 MB, so the file
  // should only ever drop the report when it genuinely cannot fit under
  // Gmail's limit — not because a lower default silently trimmed it.
  const href = `/api/runs/${runId}/export`
    + `?logs=${logs}&report=${reportOn}&max_bytes=${EXPORT_MAX_BYTES}`;

  const begin = () => {
    setStarted(true);
    window.clearTimeout(timer.current);
    // The request renders the notebook if it was never viewed, so the browser
    // shows nothing for several seconds. This is the only honest feedback
    // available — there is no progress to report.
    timer.current = window.setTimeout(() => setStarted(false), 8000);
  };

  return (
    <Modal
      title="Share this run"
      subtitle={`One self-contained HTML file${workflowName ? ` for ${workflowName}` : ''} — no link back into RunRail, nothing loaded from the network.`}
      onClose={onClose}
      wide
    >
      <div className="modal-body form-stack">
        {error && <div className="callout" style={{ color: 'var(--danger)' }}>{error}</div>}

        {!outputs && !error && <LoadingBar />}

        {outputs && (
          <>
            <div className="field">
              <span>Logs <em>stdout and stderr from every task, failed tasks first</em></span>
              <div className="segmented">
                {LOG_MODES.map(mode => (
                  <button
                    key={mode.value}
                    type="button"
                    className={clsx(logs === mode.value && 'active')}
                    onClick={() => setLogs(mode.value)}
                  >
                    {mode.label} <i>{mode.hint}</i>
                  </button>
                ))}
              </div>
            </div>

            <label className="field toggle-field">
              <span>
                Include the notebook report
                <em>
                  {hasReport
                    ? outputs.renderer_available
                      ? 'Embedded in full, or left out with a note if it will not fit'
                      : 'nbconvert is not installed here — the file will say so instead'
                    : 'This run produced no notebook'}
                </em>
              </span>
              {/* A <label>, not a <span>: the visible switch is an overlay and
                  only reaches the zero-size input through label association. */}
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={reportOn}
                  disabled={!hasReport}
                  onChange={event => setIncludeReport(event.target.checked)}
                />
                <span />
              </label>
            </label>

            <div className={clsx('share-size', `share-size--${tone}`)}>
              <strong>≈ {formatBytes(estimate)}</strong>
              <p>
                {tone === 'over'
                  ? 'Over Gmail\'s 25 MB limit. RunRail will leave the notebook report out of the file to stay under the cap and say so inside it — turn the report off yourself, or send the run link instead.'
                  : tone === 'warn'
                  ? 'Over 10 MB. Many mail servers reject attachments this size; switching logs to Tail or dropping the report usually fixes it.'
                  : 'Comfortably emailable.'}
              </p>
              <span className="share-size-parts">
                {[
                  `shell ${formatBytes(parts?.shell ?? 0)}`,
                  logs !== 'none' ? `logs ${formatBytes(parts?.logs ?? 0)}` : null,
                  reportOn ? `report ${formatBytes(parts?.report ?? 0)}` : null,
                ].filter(Boolean).join(' · ')}
              </span>
            </div>

            {logs !== 'none' ? (
              /* Not optional copy. This is the moment secrets leave the
                 machine, and nothing downstream redacts them. */
              <div className="share-warning" role="alert">
                <ShieldAlert size={16} />
                <div>
                  <strong>This file carries your logs off this machine.</strong>
                  <p>
                    Task output routinely contains API tokens, connection strings, internal
                    hostnames and customer rows. RunRail copies stdout and stderr verbatim and
                    redacts nothing. Open the file and read it before you send it to anyone.
                  </p>
                </div>
              </div>
            ) : reportOn && (
              <div className="callout">
                Logs are excluded, but the notebook report still shows whatever the notebook
                printed — query results included. Check the cells before sharing.
              </div>
            )}

            {reportOn && unrendered && (
              <div className="callout share-note">
                <Info size={13} />
                This notebook has not been rendered yet, so the export renders it first — the
                download can take a few seconds, and the real file is usually smaller than the
                estimate above.
              </div>
            )}

            {live && (
              <div className="callout share-note">
                <Info size={13} />
                This run is still {status}. Exports are frozen snapshots, so sharing is available
                once it finishes.
              </div>
            )}

            {started && (
              <div className="share-progress">
                <LoadingBar size="sm" />
                <span>Building the file. Your browser starts the download when it is ready.</span>
              </div>
            )}
          </>
        )}
      </div>

      <div className="modal-actions">
        <CancelButton>Close</CancelButton>
        {live || !outputs ? (
          <Button disabled title={live ? 'Wait for the run to finish' : 'Reading this run'}>
            <Download size={14} /> Download
          </Button>
        ) : (
          /* A plain anchor, not fetch+blob: /export already sets
             Content-Disposition with a safe filename, and letting the browser
             own the transfer keeps a 25 MB file out of JS memory. */
          <a className="btn btn-primary btn-md" href={href} download onClick={begin}>
            <Download size={14} /> Download HTML
          </a>
        )}
      </div>
    </Modal>
  );
}
