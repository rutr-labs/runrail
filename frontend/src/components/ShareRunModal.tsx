import { useEffect, useRef, useState } from 'react';
import { Download, Info, ShieldAlert } from 'lucide-react';
import clsx from 'clsx';
import { api } from '../api';
import { formatBytes } from '../format';
import { Button, CancelButton, CometCanvas, LoadingBar, Modal } from './ui';
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
  const [transferred, setTransferred] = useState(0);
  const [expected, setExpected] = useState(0);
  const abort = useRef<AbortController | null>(null);
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

  useEffect(() => () => {
    window.clearTimeout(timer.current);
    abort.current?.abort();   // closing the modal must not leave a transfer running
  }, []);

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

  /* The transfer is driven here rather than handed to a plain <a download>.
     An anchor gives the browser the file but gives us no events at all, so the
     old code guessed with an eight-second timer: an export that takes 7ms — the
     normal case, since the report is already rendered — left "Building the
     file" on screen for eight seconds after the download had finished.

     Reading the body ourselves costs one copy of a file the route already caps
     at 25MB, and buys an accurate finish plus a real percentage, because
     /export sends Content-Length. */
  const begin = async () => {
    if (started) return;
    const controller = new AbortController();
    abort.current = controller;
    setStarted(true);
    setTransferred(0);
    setExpected(0);
    setError(null);
    try {
      const response = await fetch(href, { signal: controller.signal });
      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        throw new Error(detail?.detail || `The server answered ${response.status}.`);
      }
      const total = Number(response.headers.get('Content-Length') || 0);
      setExpected(total);

      let blob: Blob;
      if (response.body) {
        const reader = response.body.getReader();
        const chunks: Uint8Array[] = [];
        let seen = 0;
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          chunks.push(value);
          seen += value.length;
          setTransferred(seen);
        }
        blob = new Blob(chunks as BlobPart[], { type: 'text/html' });
      } else {
        blob = await response.blob();   // no streaming: still correct, just no bar
      }

      // Filename from the route's own Content-Disposition, so the two cannot
      // drift; the header is already sanitised server-side.
      const disposition = response.headers.get('Content-Disposition') || '';
      const named = /filename="([^"]+)"/.exec(disposition)?.[1];
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = named || `runrail-run-${runId}.html`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      // Revoked on the next frame: revoking synchronously can cancel the save
      // in some browsers before they have read the blob.
      timer.current = window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (failure) {
      if (!controller.signal.aborted) {
        setError(failure instanceof Error ? failure.message : 'The download failed.');
      }
    } finally {
      if (abort.current === controller) abort.current = null;
      setStarted(false);
    }
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
                {expected > 0 ? (
                  <div className="wb-progress">
                    <div className="wb-progress-fill"
                         style={{ width: `${Math.min(100, (transferred / expected) * 100)}%` }}>
                      <CometCanvas kind="fill" />
                    </div>
                  </div>
                ) : <LoadingBar size="sm" />}
                <span>
                  {expected > 0
                    ? `${formatBytes(transferred)} of ${formatBytes(expected)}`
                    : 'Building the file. Large notebooks take a few seconds to render.'}
                </span>
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
          <Button onClick={begin} disabled={started}>
            <Download size={14} /> {started ? 'Preparing…' : 'Download HTML'}
          </Button>
        )}
      </div>
    </Modal>
  );
}
