// WebSocket zum Raum (SPEC Abschnitte 6 und 16) mit Neuverbindung und Uhrenabgleich.

const FATAL = {
  4401: "unauthorized",
  4429: "room_full",
  4000: "replaced",
};

export class RoomSocket extends EventTarget {
  constructor(token, clock) {
    super();
    this.token = token;
    this.clock = clock;
    this.ws = null;
    this.attempt = 0;
    this.closedByUs = false;
    this.connected = false;
    this.lastError = null;
  }

  url() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}/ws/p?token=${encodeURIComponent(this.token)}`;
  }

  connect() {
    this.closedByUs = false;
    const ws = new WebSocket(this.url());
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.connected = true;
      // kurze Serie für einen schnellen ersten Abgleich, danach alle 2 s
      clearInterval(this.syncTimer);
      let burst = 0;
      const burstTimer = setInterval(() => {
        this.send(this.clock.request());
        if (++burst >= 6) clearInterval(burstTimer);
      }, 120);
      this.syncTimer = setInterval(() => this.send(this.clock.request()), 2000);
      this.emit("open");
    };
    ws.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch { return; }
      if (msg.type === "sync") { this.clock.onSync(msg); return; }
      if (msg.type === "welcome") this.clock.seed(msg.server_ms);
      if (msg.type === "error" && (msg.code === "unauthorized" || msg.code === "room_full")) this.lastError = msg;
      this.emit("message", msg);
    };
    ws.onclose = (e) => {
      clearInterval(this.syncTimer);
      const wasConnected = this.connected;
      this.connected = false;
      if (this.ws !== ws) return;
      const fatal = FATAL[e.code];
      if (fatal) {
        this.emit("fatal", { code: fatal, message: this.lastError && this.lastError.message });
        return;
      }
      this.emit("close", { wasConnected });
      if (this.closedByUs) return;
      const wait = Math.min(5000, 300 * 2 ** this.attempt++);
      this.retryTimer = setTimeout(() => this.connect(), wait);
    };
    ws.onerror = () => { /* onclose folgt */ };
  }

  /** Sofort neu verbinden (z. B. wenn das Netz zurück ist). */
  kick() {
    if (this.connected || this.closedByUs) return;
    clearTimeout(this.retryTimer);
    if (this.ws && this.ws.readyState === WebSocket.CONNECTING) return;
    this.connect();
  }

  send(msg) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
      return true;
    }
    return false;
  }

  close() {
    this.closedByUs = true;
    clearTimeout(this.retryTimer);
    clearInterval(this.syncTimer);
    if (this.ws) this.ws.close(1000);
  }

  emit(type, detail) { this.dispatchEvent(new CustomEvent(type, { detail })); }
}
