/* ─── The live channel ─────────────────────────────────────
   One WebSocket for the whole page. The server announces every change as a
   small event ({ type: 'run_updated', id: 7 }) and pages reload what changed,
   instead of each polling on its own timer.

   It is only trusted while it is provably open: the server says "hello" when
   a connection is really established, answers each ping with a pong, and a
   connection that has said nothing for a while is assumed dead (a proxy or a
   sleeping laptop can drop one without either end noticing) and replaced.
   While it is not live, pages fall back to polling at their old pace; see
   live.ts. Events missed while it was down are made good by a "resync" after
   it reconnects, which every subscriber hears. */

type Handler = (event: Record<string, unknown>) => void;

export type SocketState = 'connecting' | 'live' | 'down';

/** How often to ping, and how long silence may last before the connection is
 *  presumed dead. The server answers every ping, so a live socket is never
 *  silent for longer than one ping interval. */
const PING_MS = 20_000;
const SILENCE_MS = 50_000;

class RunRailWS {
  private ws: WebSocket | null = null;
  private handlers = new Map<string, Set<Handler>>();
  private stateListeners = new Set<(state: SocketState) => void>();
  private reconnectDelay = 1000;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private lastHeard = 0;
  private everLive = false;
  state: SocketState = 'connecting';

  connect(): void {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/api/ws`);
    this.ws = ws;

    ws.onmessage = (e) => {
      this.lastHeard = Date.now();
      let event: Record<string, unknown>;
      try { event = JSON.parse(e.data as string) as Record<string, unknown>; } catch { return; }
      const type = String(event.type ?? '');
      if (type === 'hello') {
        this.reconnectDelay = 1000;
        this.setState('live');
        // Anything that happened while the channel was down was never heard:
        // every page reloads once. Not on the very first hello — pages load
        // their own data as they mount.
        if (this.everLive) this.emit('resync', { type: 'resync' });
        this.everLive = true;
        return;
      }
      if (type === 'pong') return;
      this.emit(type, event);
    };
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.setState('down');
      this.scheduleReconnect();
    };
    ws.onerror = () => ws.close();
  }

  private emit(type: string, event: Record<string, unknown>) {
    this.handlers.get(type)?.forEach(h => {
      try { h(event); } catch { /* one broken subscriber must not starve the rest */ }
    });
  }

  private setState(state: SocketState) {
    if (state === this.state) return;
    this.state = state;
    this.stateListeners.forEach(listener => listener(state));
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, 30_000);
      this.connect();
    }, this.reconnectDelay);
  }

  /** Keep the connection honest: ping it, and replace one that went quiet. */
  heartbeat(): void {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (this.state === 'live' && Date.now() - this.lastHeard > SILENCE_MS) {
      this.ws = null;
      this.setState('down');
      ws.close();
      this.reconnectNow();
      return;
    }
    try { ws.send('ping'); } catch { /* onclose follows */ }
  }

  /** Reconnect without waiting out the back-off: the tab came back into view,
   *  or the network did. */
  reconnectNow(): void {
    if (this.state === 'live') return;
    if (this.reconnectTimer !== null) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null; }
    this.reconnectDelay = 1000;
    this.connect();
  }

  /** Subscribe to events of a given type. Returns an unsubscribe function. */
  on(type: string, handler: Handler): () => void {
    if (!this.handlers.has(type)) this.handlers.set(type, new Set());
    this.handlers.get(type)!.add(handler);
    return () => this.handlers.get(type)?.delete(handler);
  }

  /** Follow the connection's state. Returns an unsubscribe function. */
  onState(listener: (state: SocketState) => void): () => void {
    this.stateListeners.add(listener);
    return () => this.stateListeners.delete(listener);
  }
}

export const rrws = new RunRailWS();
rrws.connect();
window.setInterval(() => rrws.heartbeat(), PING_MS);
document.addEventListener('visibilitychange', () => { if (!document.hidden) rrws.reconnectNow(); });
window.addEventListener('online', () => rrws.reconnectNow());
