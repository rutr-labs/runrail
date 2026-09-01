import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Copy, Check, Search, ChevronUp, ChevronDown, X } from 'lucide-react';
import clsx from 'clsx';

interface LogViewerProps {
  taskRunId: number;
  taskStatus?: string;
  initialTab?: 'stdout' | 'stderr';
  errorMessage?: string;
}

/* ─── ANSI (SGR) parsing ──────────────────────────────────
   Real script output is full of color escapes that would otherwise render
   as "[32m" noise. Supports the 16 basic colors, 256-color (38;5;n), bold,
   dim, italic and underline; every other escape sequence is stripped. */

type Segment = { text: string; style?: React.CSSProperties };

const BASIC: string[] = [
  '#3f4451', '#e05561', '#8cc265', '#d18f52', '#4aa5f0', '#c162de', '#42b3c2', '#d7dae0',
  '#4f5666', '#ff616e', '#a5e075', '#f0a45d', '#4dc4ff', '#de73ff', '#4cd1e0', '#ffffff',
];

function color256(n: number): string {
  if (n < 16) return BASIC[n];
  if (n < 232) {
    const v = n - 16;
    const scale = [0, 95, 135, 175, 215, 255];
    return `rgb(${scale[Math.floor(v / 36)]}, ${scale[Math.floor(v / 6) % 6]}, ${scale[v % 6]})`;
  }
  const gray = 8 + (n - 232) * 10;
  return `rgb(${gray}, ${gray}, ${gray})`;
}

function parseAnsi(input: string): Segment[] {
  const segments: Segment[] = [];
  let style: React.CSSProperties = {};
  let buffer = '';
  const flush = () => {
    if (buffer) segments.push({ text: buffer, style: Object.keys(style).length ? { ...style } : undefined });
    buffer = '';
  };
  // eslint-disable-next-line no-control-regex
  const tokens = input.split(/(\x1b\[[0-9;]*m|\x1b\[[0-9;?]*[A-LN-Za-ln-z])/);
  for (const token of tokens) {
    if (!token) continue;
    if (!token.startsWith('\x1b[')) { buffer += token; continue; }
    if (!token.endsWith('m')) continue; // strip cursor/erase sequences
    flush();
    const codes = token.slice(2, -1).split(';').map(v => parseInt(v || '0', 10));
    for (let i = 0; i < codes.length; i++) {
      const code = codes[i];
      if (code === 0) style = {};
      else if (code === 1) style.fontWeight = 700;
      else if (code === 2) style.opacity = 0.65;
      else if (code === 3) style.fontStyle = 'italic';
      else if (code === 4) style.textDecoration = 'underline';
      else if (code >= 30 && code <= 37) style.color = BASIC[code - 30];
      else if (code >= 90 && code <= 97) style.color = BASIC[code - 90 + 8];
      else if (code >= 40 && code <= 47) style.backgroundColor = BASIC[code - 40];
      else if (code === 39) delete style.color;
      else if (code === 49) delete style.backgroundColor;
      else if (code === 38 && codes[i + 1] === 5) { style.color = color256(codes[i + 2] ?? 0); i += 2; }
      else if (code === 48 && codes[i + 1] === 5) { style.backgroundColor = color256(codes[i + 2] ?? 0); i += 2; }
      else if (code === 38 && codes[i + 1] === 2) { style.color = `rgb(${codes[i + 2]},${codes[i + 3]},${codes[i + 4]})`; i += 4; }
      else if (code === 48 && codes[i + 1] === 2) { style.backgroundColor = `rgb(${codes[i + 2]},${codes[i + 3]},${codes[i + 4]})`; i += 4; }
    }
  }
  flush();
  return segments;
}

const ANSI_LIMIT = 2_000_000; // beyond this, render plain for responsiveness

export function LogViewer({ taskRunId, taskStatus, initialTab = 'stdout', errorMessage }: LogViewerProps) {
  const [log, setLog] = useState<string>('');
  const [tab, setTab] = useState<'stdout' | 'stderr'>(initialTab);
  const [loading, setLoading] = useState(true);
  const [copied, setCopied] = useState(false);
  const [query, setQuery] = useState('');
  const [activeMatch, setActiveMatch] = useState(0);
  const preRef = useRef<HTMLPreElement>(null);
  const isRunning = taskStatus === 'running';

  // The effect below owns fetching for a finished task; the tab buttons only
  // change the tab. Fetching here as well downloaded every log twice per click,
  // and with no stale guard a slow stdout response could land after a switch to
  // stderr and overwrite it.

  // Follow the tail while streaming, but never fight the user: only stick to
  // the bottom if they were already reading the bottom.
  //
  // Stickiness is measured on scroll, BEFORE new output arrives. Measuring it
  // after the append (in an effect keyed on `log`) measures the chunk just
  // added, so any burst taller than the threshold — about six lines, and the
  // server batches 250ms of output per message — read as "the user scrolled
  // away" and following died silently for the rest of the task.
  const stuckToBottom = useRef(true);
  const onLogScroll = useCallback(() => {
    const pre = preRef.current;
    if (pre) stuckToBottom.current = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 120;
  }, []);
  useLayoutEffect(() => {
    const pre = preRef.current;
    if (pre && isRunning && stuckToBottom.current) pre.scrollTop = pre.scrollHeight;
  }, [log, isRunning]);

  useEffect(() => {
    if (!isRunning) {
      let stale = false;
      setLoading(true);
      fetch(`/api/task-runs/${taskRunId}/${tab}`)
        .then(r => r.text())
        .then(text => { if (!stale) { setLog(text); setLoading(false); } })
        .catch(() => { if (!stale) { setLog(''); setLoading(false); } });
      return () => { stale = true; };
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
    ws.onerror = () => {  // fall back to HTTP on WS error
      setLoading(true);
      fetch(`/api/task-runs/${taskRunId}/${tab}`)
        .then(r => r.text())
        .then(text => { setLog(text); setLoading(false); })
        .catch(() => { setLog(''); setLoading(false); });
    };
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

  const segments = useMemo(
    () => (log.length > ANSI_LIMIT ? [{ text: log }] : parseAnsi(log)),
    [log],
  );
  const matchCount = useMemo(() => {
    if (!query) return 0;
    let count = 0;
    const needle = query.toLowerCase();
    for (const segment of segments) {
      let at = segment.text.toLowerCase().indexOf(needle);
      while (at !== -1) { count++; at = segment.text.toLowerCase().indexOf(needle, at + needle.length); }
    }
    return count;
  }, [segments, query]);

  useEffect(() => setActiveMatch(0), [query, tab]);
  useEffect(() => {
    preRef.current?.querySelector('.log-match-active')?.scrollIntoView({ block: 'center' });
  }, [activeMatch, matchCount]);

  const step = (delta: number) => {
    if (matchCount) setActiveMatch(m => (m + delta + matchCount) % matchCount);
  };

  const renderSegments = () => {
    if (!query) {
      return segments.map((s, i) => s.style ? <span key={i} style={s.style}>{s.text}</span> : s.text);
    }
    const needle = query.toLowerCase();
    let matchIndex = 0;
    return segments.map((segment, i) => {
      const parts: React.ReactNode[] = [];
      const lower = segment.text.toLowerCase();
      let cursor = 0;
      let at = lower.indexOf(needle);
      while (at !== -1) {
        if (at > cursor) parts.push(segment.text.slice(cursor, at));
        const current = matchIndex++;
        parts.push(
          <mark key={`m${i}-${at}`} className={clsx('log-match', current === activeMatch && 'log-match-active')}>
            {segment.text.slice(at, at + query.length)}
          </mark>
        );
        cursor = at + query.length;
        at = lower.indexOf(needle, cursor);
      }
      parts.push(segment.text.slice(cursor));
      return segment.style ? <span key={i} style={segment.style}>{parts}</span> : <span key={i}>{parts}</span>;
    });
  };

  return (
    <div className="log-viewer">
      <div className="log-viewer-header">
        <div className="log-tabs">
          <button
            className={clsx('log-tab', tab === 'stdout' && 'log-tab--active')}
            onClick={() => setTab('stdout')}
          >
            stdout
          </button>
          <button
            className={clsx('log-tab', tab === 'stderr' && 'log-tab--active')}
            onClick={() => setTab('stderr')}
          >
            stderr
          </button>
        </div>
        <div className="log-toolbar">
          <div className="log-search">
            <Search size={12} />
            <input
              value={query}
              placeholder="Search…"
              onChange={e => setQuery(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter') { e.preventDefault(); step(e.shiftKey ? -1 : 1); }
                if (e.key === 'Escape') setQuery('');
              }}
              aria-label="Search logs"
            />
            {query && (
              <>
                <span className="log-search-count">{matchCount ? `${activeMatch + 1}/${matchCount}` : '0'}</span>
                <button onClick={() => step(-1)} aria-label="Previous match"><ChevronUp size={12} /></button>
                <button onClick={() => step(1)} aria-label="Next match"><ChevronDown size={12} /></button>
                <button onClick={() => setQuery('')} aria-label="Clear search"><X size={12} /></button>
              </>
            )}
          </div>
          <button className="log-copy-btn" onClick={copy} title="Copy logs">
            {copied ? <><Check size={13} /> Copied</> : <><Copy size={13} /> Copy</>}
          </button>
        </div>
      </div>
      {errorMessage && (
        <div className="log-error-banner">
          <span className="log-error-label">Error</span>
          {errorMessage}
        </div>
      )}
      <pre ref={preRef} onScroll={onLogScroll}
           className={clsx('log-content', loading && 'log-loading')}>
        {loading
          ? 'Loading…'
          : log.trim()
          ? renderSegments()
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
