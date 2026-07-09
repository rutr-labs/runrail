import { useEffect, useRef, useState } from 'react';
import { Copy, Check } from 'lucide-react';
import clsx from 'clsx';

interface LogViewerProps {
  taskRunId: number;
  taskStatus?: string;
  initialTab?: 'stdout' | 'stderr';
  errorMessage?: string;
}

export function LogViewer({ taskRunId, taskStatus, initialTab = 'stdout', errorMessage }: LogViewerProps) {
  const [log, setLog] = useState<string>('');
  const [tab, setTab] = useState<'stdout' | 'stderr'>(initialTab);
  const [loading, setLoading] = useState(true);
  const [copied, setCopied] = useState(false);
  const preRef = useRef<HTMLPreElement>(null);
  const isRunning = taskStatus === 'running';

  const loadLog = (t: 'stdout' | 'stderr') => {
    setLoading(true);
    setTab(t);
    fetch(`/api/task-runs/${taskRunId}/${t}`)
      .then(r => r.text())
      .then(text => { setLog(text); setLoading(false); })
      .catch(() => { setLog(''); setLoading(false); });
  };

  // Follow the tail while streaming, but never fight the user: only stick to
  // the bottom if they were already reading the bottom.
  useEffect(() => {
    const pre = preRef.current;
    if (!pre || !isRunning) return;
    const nearBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 120;
    if (nearBottom) pre.scrollTop = pre.scrollHeight;
  }, [log, isRunning]);

  useEffect(() => {
    if (!isRunning) {
      loadLog(tab);
      return;
    }
    // Streaming mode: open a WebSocket that tails the log file until the task finishes.
    setLog('');
    setLoading(true);
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(
      `${proto}//${location.host}/api/ws/task-runs/${taskRunId}/logs?stream=${tab}`
    );
    ws.onopen = () => setLoading(false);
    ws.onmessage = (e) => setLog(prev => prev + (e.data as string));
    // When the server closes the socket (task done), do a final fetch for the complete log.
    ws.onclose = () => {
      fetch(`/api/task-runs/${taskRunId}/${tab}`)
        .then(r => r.text())
        .then(text => setLog(text))
        .catch(() => { /* keep whatever was streamed */ });
    };
    ws.onerror = () => loadLog(tab); // fall back to HTTP on WS error
    return () => {
      ws.onclose = null; // prevent the final fetch when we close intentionally
      ws.close();
    };
  }, [taskRunId, tab, isRunning]);

  const copy = () => {
    navigator.clipboard.writeText(log).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };

  return (
    <div className="log-viewer">
      <div className="log-viewer-header">
        <div className="log-tabs">
          <button
            className={clsx('log-tab', tab === 'stdout' && 'log-tab--active')}
            onClick={() => isRunning ? setTab('stdout') : loadLog('stdout')}
          >
            stdout
          </button>
          <button
            className={clsx('log-tab', tab === 'stderr' && 'log-tab--active')}
            onClick={() => isRunning ? setTab('stderr') : loadLog('stderr')}
          >
            stderr
          </button>
        </div>
        <button className="log-copy-btn" onClick={copy} title="Copy logs">
          {copied ? <><Check size={13} /> Copied</> : <><Copy size={13} /> Copy</>}
        </button>
      </div>
      {errorMessage && (
        <div className="log-error-banner">
          <span className="log-error-label">Error</span>
          {errorMessage}
        </div>
      )}
      <pre ref={preRef} className={clsx('log-content', loading && 'log-loading')}>
        {loading
          ? 'Loading…'
          : log.trim()
          ? log
          : isRunning
          ? ''
          : tab === 'stderr'
          ? '(no stderr output captured)'
          : '(no stdout output captured)'}
        {isRunning && !loading && <span className="log-caret" aria-hidden />}
      </pre>
    </div>
  );
}
