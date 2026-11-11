// Schnittansicht: Wellenform der ungeschnittenen Mischung (Canvas aus peaks.json), Abspielen aus audio/mix.
// Zeiten in Sitzungszeit (ms). Zeigt Kapitelmarken, Sound-Ereignisse, bestehende Schnitte (schraffiert), Trimmgriffe,
// markiert Bereiche per Ziehen oder Tasten (I, O), schneidet heraus, macht rückgängig, hört über Schnitte hinweg vor.
import { button, clear, fmtClock, fmtDuration, h, icon, t } from "./common.js";
import * as E from "./edl.js";

const TICKS_S = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
const MAX_PPS = 400;

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export class CutEditor {
  /**
   * @param {object} o
   * @param {string} o.audioUrl        ungeschnittene Mischung
   * @param {object} o.peaks           {sample_rate, samples_per_peak, peaks: [min, max, ...]}
   * @param {object} o.edl             gespeicherte Schnittliste
   * @param {Array}  o.lines           [{ms, label, kind: "chapter" | "sound"}]
   * @param {Array}  o.pauses          [{from_ms, to_ms}] Pausen aus den Ereignissen (nur zur Beschriftung)
   * @param {Function} o.onChange      (edl, dirty) => void
   */
  constructor(o) {
    this.o = o;
    const pk = o.peaks;
    this.peaks = pk.peaks;
    this.msPerPeak = (pk.samples_per_peak / pk.sample_rate) * 1000;
    this.total = Math.max(1000, Math.round((this.peaks.length / 2) * this.msPerPeak));
    let maxAbs = 0;
    for (let i = 0; i < this.peaks.length; i += 7) maxAbs = Math.max(maxAbs, Math.abs(this.peaks[i]));
    const unit = maxAbs <= 1.0001 ? 1 : 128;
    this.unit = unit;
    this.amp = 0.94 / Math.max(0.2, Math.min(1, maxAbs / unit));
    this.edl = E.normalize(o.edl || E.emptyEdl(), this.total);
    this.saved = JSON.stringify(this.edl);
    this.history = [];
    this.lines = (o.lines || []).filter((l) => l.ms >= 0 && l.ms <= this.total + 5000).sort((a, b) => a.ms - b.ms);
    this.pauses = o.pauses || [];
    this.view = { start: 0, pps: 1 };
    this.pos = 0;
    this.sel = null;
    this.markA = null;
    this.selCut = -1;
    this.skip = true;
    this.preview = null;
    this.playing = false;
    this.drag = null;
    this.w = 0;
    this.h = 0;
    this.raf = 0;
    this.destroyed = false;
    this.colors = {};
    this._build();
    this._bind();
    this._resize();
    this.fit();
    this._updateAll();
    document.fonts?.ready.then(() => this.draw());
  }

  // --- Aufbau -------------------------------------------------------------------------------
  _build() {
    const play = (this.btnPlay = button(t("Abspielen"), { icon: "play", onclick: () => this.toggle(), title: t("Abspielen und Anhalten (Leertaste)") }));
    this.readout = h("p", { class: "m-ed-time m-num", "aria-label": t("Position und Länge") });
    this.skipBox = h("input", { type: "checkbox", id: "m-ed-skip", checked: true });
    this.skipBox.addEventListener("change", () => (this.skip = this.skipBox.checked));
    const transport = h("div", { class: "m-ed-bar" },
      play,
      button("−5 s", { onclick: () => this.seek(this.pos - 5000), ariaLabel: t("5 Sekunden zurück"), title: t("5 Sekunden zurück (Pfeil links)") }),
      button("+5 s", { onclick: () => this.seek(this.pos + 5000), ariaLabel: t("5 Sekunden vor"), title: t("5 Sekunden vor (Pfeil rechts)") }),
      this.readout,
      h("label", { class: "m-check m-ed-skip", for: "m-ed-skip" }, this.skipBox, h("span", null, t("Geschnittene Bereiche überspringen"))));

    this.main = h("canvas", { class: "m-ed-main", role: "img", "aria-label": t("Wellenform der ungeschnittenen Mischung") });
    this.stage = h("div", { class: "m-ed-stage", tabindex: "0", "aria-describedby": "m-ed-keys" }, this.main);
    this.over = h("canvas", { class: "m-ed-over", "aria-hidden": "true" });

    this.btnIn = button(t("Beginn setzen"), { onclick: () => this.markIn(), title: t("Beginn des Bereichs an der Abspielposition (I)") });
    this.btnOut = button(t("Ende setzen"), { onclick: () => this.markOut(), title: t("Ende des Bereichs an der Abspielposition (O)") });
    this.btnHear = button(t("Übergang hören"), { icon: "play", onclick: () => this.hearSelection(), title: t("Vorhören mit herausgeschnittenem Bereich") });
    this.btnCut = button(t("Herausschneiden"), { icon: "scissors", onclick: () => this.cutSelection(), title: t("Markierten Bereich herausschneiden (Entf)") });
    this.btnRemove = button(t("Schnitt entfernen"), { onclick: () => this.removeSelectedCut(), title: t("Gewählten Schnitt entfernen (Entf)") });
    this.btnUndo = button(t("Rückgängig"), { icon: "undo", onclick: () => this.undo(), title: t("Letzte Änderung zurücknehmen (Strg+Z)") });
    const tools = h("div", { class: "m-ed-bar m-ed-tools" },
      h("div", { class: "m-ed-group", role: "group", "aria-label": t("Zoom") },
        button("", { icon: "zoomOut", ariaLabel: t("Verkleinern"), title: t("Verkleinern (Minus)"), onclick: () => this.zoomBy(0.5) }),
        button("", { icon: "zoomIn", ariaLabel: t("Vergrößern"), title: t("Vergrößern (Plus)"), onclick: () => this.zoomBy(2) }),
        button(t("Alles"), { icon: "fit", title: t("Ganze Aufnahme zeigen"), onclick: () => this.fit() })),
      h("div", { class: "m-ed-group", role: "group", "aria-label": t("Bereich markieren") }, this.btnIn, this.btnOut, this.btnHear, this.btnCut),
      h("div", { class: "m-ed-group", role: "group", "aria-label": t("Änderungen") }, this.btnRemove, this.btnUndo));

    this.summary = h("p", { class: "m-ed-summary", "aria-live": "polite" });
    this.cutsList = h("div", { class: "m-ed-cuts" });
    const k = (...keys) => keys.map((x) => h("kbd", { class: "app-kbd" }, x));
    const pair = (keys, text) => h("li", null, h("span", { class: "m-keys-caps" }, keys), h("span", null, text));
    this.keys = h("div", { id: "m-ed-keys", class: "tr-small m-hint" },
      h("ul", { class: "m-keys", "aria-label": t("Tasten") },
        pair(k(t("Leertaste")), t("Abspielen")),
        pair(k("←", "→"), t("5 Sekunden")),
        pair(k(t("Umschalt"), "←", "→"), t("1 Sekunde")),
        pair(k("I", "O"), t("Beginn und Ende setzen")),
        pair(k(t("Entf")), t("Herausschneiden oder Schnitt entfernen")),
        pair(k(t("Strg"), "Z"), t("Rückgängig")),
        pair(k("+", "−"), t("Zoom"))),
      h("p", null, t("Ziehen markiert einen Bereich, die senkrechten Griffe an Anfang und Ende trimmen")));
    this.audio = h("audio", { preload: "auto", src: this.o.audioUrl });
    this.audioError = h("p", { class: "tr-error", role: "alert", hidden: true }, t("Die Mischung lässt sich nicht abspielen. Die Wellenform steht trotzdem zum Schneiden bereit."));

    this.el = h("div", { class: "m-editor" }, transport, this.stage, this.over, tools, this.summary, this.audioError, this.cutsList, this.keys, this.audio);
  }

  _bind() {
    const a = this.audio;
    a.addEventListener("loadedmetadata", () => {
      if (Number.isFinite(a.duration) && a.duration * 1000 > this.total + 2000) {
        // Mischung länger als die Wellenform (sollte nicht vorkommen): nur Anzeige erweitern
        this.total = Math.round(a.duration * 1000);
        this.fit();
      }
      this._updateAll();
    });
    a.addEventListener("error", () => (this.audioError.hidden = false));
    a.addEventListener("ended", () => this._stopped());
    a.addEventListener("pause", () => {
      if (this.playing) this._stopped();
    });
    a.addEventListener("seeked", () => {
      this.pos = a.currentTime * 1000;
      this.draw();
    });

    const m = this.main;
    m.addEventListener("pointerdown", (e) => this._down(e));
    m.addEventListener("pointermove", (e) => this._move(e));
    m.addEventListener("pointerup", (e) => this._up(e));
    m.addEventListener("pointercancel", (e) => this._up(e, true));
    m.addEventListener("wheel", (e) => this._wheel(e), { passive: false });
    const ov = this.over;
    ov.addEventListener("pointerdown", (e) => this._overDown(e));
    ov.addEventListener("pointermove", (e) => this.overDrag && this._overMove(e));
    ov.addEventListener("pointerup", () => (this.overDrag = false));
    ov.addEventListener("pointercancel", () => (this.overDrag = false));

    this._onKey = (e) => this._key(e);
    document.addEventListener("keydown", this._onKey);
    this.ro = new ResizeObserver(() => {
      this._resize();
      this.draw();
    });
    this.ro.observe(this.stage);
  }

  destroy() {
    this.destroyed = true;
    cancelAnimationFrame(this.raf);
    document.removeEventListener("keydown", this._onKey);
    this.ro.disconnect();
    this.audio.pause();
    this.audio.removeAttribute("src");
    this.audio.load();
  }

  // --- Größe, Koordinaten -------------------------------------------------------------------
  _resize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.stage.getBoundingClientRect();
    const w = Math.max(200, Math.floor(rect.width));
    const hh = window.innerWidth < 640 ? 148 : 188;
    this.w = w;
    this.h = hh;
    this.dpr = dpr;
    for (const [c, ch] of [[this.main, hh], [this.over, 44]]) {
      c.width = Math.round(w * dpr);
      c.height = Math.round(ch * dpr);
      c.style.height = ch + "px";
    }
    this.oh = 44;
    this.colors = {
      // Farben kommen aus dem Thema; die Ersatzwerte greifen nur, wenn eine Variable fehlt
      ink: css("--ink") || "#111827", slate: css("--slate") || "#4B5565", line: css("--line") || "#DDE1E8",
      paper: css("--paper") || "#F5F7FA", surface: css("--surface") || "#FFFFFF", tint: css("--petrol-tint") || "#E8EDFB",
      muted: css("--muted") || "#6B7487",
    };
    this.hatch = this._pattern();
    this._clampView();
  }

  _pattern() {
    const c = document.createElement("canvas");
    c.width = c.height = 8 * this.dpr;
    const x = c.getContext("2d");
    x.scale(this.dpr, this.dpr);
    x.fillStyle = this.colors.paper;
    x.fillRect(0, 0, 8, 8);
    x.strokeStyle = this.colors.slate;
    x.globalAlpha = 0.55;
    x.lineWidth = 1;
    x.beginPath();
    x.moveTo(-1, 9);
    x.lineTo(9, -1);
    x.moveTo(-1, 1);
    x.lineTo(1, -1);
    x.moveTo(7, 9);
    x.lineTo(9, 7);
    x.stroke();
    const pat = this.main.getContext("2d").createPattern(c, "repeat");
    if (pat && pat.setTransform) pat.setTransform(new DOMMatrix().scale(1 / this.dpr));
    return pat;
  }

  get visibleMs() {
    return (this.w / this.view.pps) * 1000;
  }

  _clampView() {
    const minPps = this.w / (this.total / 1000);
    this.view.pps = Math.min(MAX_PPS, Math.max(minPps, this.view.pps));
    this.view.start = Math.max(0, Math.min(this.view.start, this.total - this.visibleMs));
    if (this.total <= this.visibleMs) this.view.start = 0;
  }

  x2ms(x) {
    return this.view.start + (x / this.view.pps) * 1000;
  }

  ms2x(ms) {
    return ((ms - this.view.start) / 1000) * this.view.pps;
  }

  fit() {
    this.view.pps = this.w / (this.total / 1000);
    this.view.start = 0;
    this._clampView();
    this.draw();
  }

  zoomBy(f, anchorMs) {
    const anchor = anchorMs ?? (this.pos >= this.view.start && this.pos <= this.view.start + this.visibleMs ? this.pos : this.view.start + this.visibleMs / 2);
    const ax = this.ms2x(anchor);
    this.view.pps *= f;
    this._clampView();
    this.view.start = anchor - (ax / this.view.pps) * 1000;
    this._clampView();
    this.draw();
  }

  scrollTo(ms, centered = true) {
    this.view.start = centered ? ms - this.visibleMs / 2 : ms;
    this._clampView();
    this.draw();
  }

  /** Zeigt den Bereich [from, to] so, dass er mit Rand sichtbar ist. */
  showRange(from, to) {
    const span = Math.max(to - from, 4000) * 1.6;
    const pps = (this.w / span) * 1000;
    this.view.pps = Math.min(pps, MAX_PPS);
    this._clampView();
    this.scrollTo((from + to) / 2);
  }

  // --- Zeichnen -----------------------------------------------------------------------------
  draw() {
    if (this.destroyed || !this.w) return;
    this._drawMain();
    this._drawOver();
  }

  _drawMain() {
    const ctx = this.main.getContext("2d");
    const { w, h: H, dpr, colors: c } = this;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = c.surface;
    ctx.fillRect(0, 0, w, H);
    const top = 22;
    const bottom = H - 22;
    const mid = (top + bottom) / 2;
    const half = (bottom - top) / 2;
    const { start, pps } = this.view;
    const msPerPx = 1000 / pps;

    // Schnitte, schraffiert, unter der Wellenform
    ctx.fillStyle = this.hatch;
    this.edl.cuts.forEach((cut, i) => {
      const x0 = this.ms2x(cut.from_ms);
      const x1 = this.ms2x(cut.to_ms);
      if (x1 < 0 || x0 > w) return;
      ctx.fillStyle = this.hatch;
      ctx.fillRect(x0, top - 4, x1 - x0, bottom - top + 8);
      ctx.strokeStyle = i === this.selCut ? c.ink : c.slate;
      ctx.lineWidth = i === this.selCut ? 2 : 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(x0) + 0.5, top - 4);
      ctx.lineTo(Math.round(x0) + 0.5, bottom + 4);
      ctx.moveTo(Math.round(x1) + 0.5, top - 4);
      ctx.lineTo(Math.round(x1) + 0.5, bottom + 4);
      ctx.stroke();
    });

    // Auswahl
    if (this.sel) {
      const x0 = this.ms2x(this.sel.from);
      const x1 = this.ms2x(this.sel.to);
      ctx.fillStyle = c.tint;
      ctx.fillRect(x0, top - 4, x1 - x0, bottom - top + 8);
    }

    // Wellenform
    ctx.strokeStyle = c.ink;
    ctx.lineWidth = 1;
    ctx.beginPath();
    const n = this.peaks.length / 2;
    for (let x = 0; x < w; x++) {
      const t0 = start + x * msPerPx;
      const t1 = t0 + msPerPx;
      let i0 = Math.floor(t0 / this.msPerPeak);
      let i1 = Math.max(i0 + 1, Math.ceil(t1 / this.msPerPeak));
      if (i1 <= 0 || i0 >= n) continue;
      i0 = Math.max(0, i0);
      i1 = Math.min(n, i1);
      let mn = 1;
      let mx = -1;
      for (let i = i0; i < i1; i++) {
        const a = this.peaks[2 * i];
        const b = this.peaks[2 * i + 1];
        if (a < mn) mn = a;
        if (b > mx) mx = b;
      }
      mn = (mn / this.unit) * this.amp;
      mx = (mx / this.unit) * this.amp;
      const yTop = mid - Math.min(1, mx) * half;
      const yBot = mid - Math.max(-1, mn) * half;
      ctx.moveTo(x + 0.5, yTop);
      ctx.lineTo(x + 0.5, Math.max(yBot, yTop + 1));
    }
    ctx.stroke();
    ctx.strokeStyle = c.line;
    ctx.beginPath();
    ctx.moveTo(0, mid + 0.5);
    ctx.lineTo(w, mid + 0.5);
    ctx.stroke();

    // Außerhalb der Trimmgriffe abblenden
    const te = E.endOf(this.edl, this.total);
    ctx.fillStyle = "rgba(247,247,245,0.82)";
    const xs = this.ms2x(this.edl.trim_start_ms);
    const xe = this.ms2x(te);
    if (xs > 0) ctx.fillRect(0, top - 4, Math.min(w, xs), bottom - top + 8);
    if (xe < w) ctx.fillRect(Math.max(0, xe), top - 4, w - Math.max(0, xe), bottom - top + 8);

    this._drawLines(ctx, top, bottom);
    this._drawRuler(ctx, bottom);

    // Beginnmarke
    if (this.markA !== null) {
      const x = this.ms2x(this.markA);
      ctx.strokeStyle = c.ink;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(x + 0.5, top - 4);
      ctx.lineTo(x + 0.5, bottom + 4);
      ctx.stroke();
      ctx.setLineDash([]);
      this._label(ctx, t("Beginn"), x + 4, bottom - 8, c.ink);
    }
    // Auswahlkanten
    if (this.sel) {
      ctx.strokeStyle = c.ink;
      ctx.lineWidth = 1.5;
      for (const ms of [this.sel.from, this.sel.to]) {
        const x = Math.round(this.ms2x(ms)) + 0.5;
        ctx.beginPath();
        ctx.moveTo(x, top - 4);
        ctx.lineTo(x, bottom + 4);
        ctx.stroke();
      }
    }
    // Trimmgriffe
    this._handle(ctx, xs, mid, top, bottom, "start");
    this._handle(ctx, xe, mid, top, bottom, "end");
    // Abspielkopf
    const px = this.ms2x(this.pos);
    if (px >= -2 && px <= w + 2) {
      ctx.strokeStyle = c.ink;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(px, 0);
      ctx.lineTo(px, H - 18);
      ctx.stroke();
      ctx.fillStyle = c.ink;
      ctx.beginPath();
      ctx.moveTo(px - 5, 0);
      ctx.lineTo(px + 5, 0);
      ctx.lineTo(px, 7);
      ctx.fill();
    }
    ctx.strokeStyle = c.line;
    ctx.lineWidth = 1;
    ctx.strokeRect(0.5, 0.5, w - 1, H - 1);
  }

  _handle(ctx, x, mid, top, bottom, side) {
    if (x < -12 || x > this.w + 12) return;
    const c = this.colors;
    ctx.strokeStyle = c.ink;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(Math.round(x), top - 4);
    ctx.lineTo(Math.round(x), bottom + 4);
    ctx.stroke();
    const gw = 10;
    const gx = side === "start" ? x : x - gw;
    ctx.fillStyle = c.ink;
    ctx.fillRect(gx, mid - 14, gw, 28);
    ctx.strokeStyle = c.surface;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(gx + 3.5, mid - 6);
    ctx.lineTo(gx + 3.5, mid + 6);
    ctx.moveTo(gx + 6.5, mid - 6);
    ctx.lineTo(gx + 6.5, mid + 6);
    ctx.stroke();
  }

  _label(ctx, text, x, y, color, align = "left") {
    ctx.font = '500 11px "IBM Plex Sans", Arial, sans-serif';
    ctx.textBaseline = "middle";
    const tw = ctx.measureText(text).width;
    const x0 = align === "left" ? x : x - tw;
    ctx.fillStyle = this.colors.surface;
    ctx.globalAlpha = 0.9;
    ctx.fillRect(x0 - 2, y - 7, tw + 4, 14);
    ctx.globalAlpha = 1;
    ctx.fillStyle = color;
    ctx.textAlign = "left";
    ctx.fillText(text, x0, y);
    return x0 + tw + 6;
  }

  _drawLines(ctx, top, bottom) {
    const c = this.colors;
    let rightTop = -1e9;
    let rightBot = -1e9;
    for (const l of this.lines) {
      const x = this.ms2x(l.ms);
      if (x < -2 || x > this.w + 2) continue;
      const chapter = l.kind === "chapter";
      ctx.strokeStyle = chapter ? c.ink : c.slate;
      ctx.lineWidth = 1;
      ctx.setLineDash(chapter ? [] : [3, 3]);
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, chapter ? 10 : top);
      ctx.lineTo(Math.round(x) + 0.5, chapter ? bottom : bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      const text = l.label.length > 24 ? l.label.slice(0, 23) + "…" : l.label;
      ctx.font = '500 11px "IBM Plex Sans", Arial, sans-serif';
      const tw = ctx.measureText(text).width;
      if (chapter) {
        if (x + 3 > rightTop) rightTop = this._label(ctx, text, x + 3, 9, c.ink);
      } else if (x + 3 > rightBot) {
        rightBot = this._label(ctx, text, x + 3, bottom - 9, c.slate);
      }
      void tw;
    }
  }

  _drawRuler(ctx, bottom) {
    const c = this.colors;
    const pps = this.view.pps;
    const step = TICKS_S.find((s) => s * pps >= 84) || TICKS_S[TICKS_S.length - 1];
    ctx.font = '400 11px "IBM Plex Sans", Arial, sans-serif';
    ctx.textBaseline = "middle";
    ctx.textAlign = "left";
    ctx.fillStyle = c.slate;
    ctx.strokeStyle = c.muted;
    ctx.lineWidth = 1;
    const first = Math.ceil(this.view.start / 1000 / step) * step;
    ctx.beginPath();
    for (let s = first; s * 1000 <= this.view.start + this.visibleMs; s += step) {
      const x = Math.round(this.ms2x(s * 1000)) + 0.5;
      ctx.moveTo(x, bottom + 6);
      ctx.lineTo(x, bottom + 12);
    }
    ctx.stroke();
    for (let s = first; s * 1000 <= this.view.start + this.visibleMs; s += step) {
      const x = this.ms2x(s * 1000);
      ctx.fillText(fmtClock(s * 1000), x + 3, bottom + 14);
    }
  }

  _drawOver() {
    const ctx = this.over.getContext("2d");
    const { w, dpr, oh, colors: c } = this;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = c.surface;
    ctx.fillRect(0, 0, w, oh);
    const n = this.peaks.length / 2;
    const mid = oh / 2;
    ctx.strokeStyle = c.muted;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let x = 0; x < w; x++) {
      const i0 = Math.floor(((x / w) * this.total) / this.msPerPeak);
      const i1 = Math.max(i0 + 1, Math.ceil((((x + 1) / w) * this.total) / this.msPerPeak));
      let mn = 1;
      let mx = -1;
      for (let i = Math.max(0, i0); i < Math.min(n, i1); i++) {
        if (this.peaks[2 * i] < mn) mn = this.peaks[2 * i];
        if (this.peaks[2 * i + 1] > mx) mx = this.peaks[2 * i + 1];
      }
      if (mx < mn) continue;
      mn = (mn / this.unit) * this.amp;
      mx = (mx / this.unit) * this.amp;
      ctx.moveTo(x + 0.5, mid - Math.min(1, mx) * (mid - 3));
      ctx.lineTo(x + 0.5, Math.max(mid - Math.max(-1, mn) * (mid - 3), mid - Math.min(1, mx) * (mid - 3) + 1));
    }
    ctx.stroke();
    const sx = (ms) => (ms / this.total) * w;
    ctx.fillStyle = "rgba(247,247,245,0.85)";
    for (const cut of this.edl.cuts) ctx.fillRect(sx(cut.from_ms), 0, Math.max(1, sx(cut.to_ms) - sx(cut.from_ms)), oh);
    ctx.fillRect(0, 0, sx(this.edl.trim_start_ms), oh);
    ctx.fillRect(sx(E.endOf(this.edl, this.total)), 0, w, oh);
    ctx.strokeStyle = c.slate;
    for (const cut of this.edl.cuts) {
      ctx.beginPath();
      ctx.moveTo(sx(cut.from_ms) + 0.5, oh - 6);
      ctx.lineTo(sx(cut.from_ms) + 0.5, oh);
      ctx.stroke();
    }
    ctx.strokeStyle = c.ink;
    ctx.lineWidth = 1.5;
    const vx = sx(this.view.start);
    const vw = Math.max(6, sx(Math.min(this.visibleMs, this.total)));
    ctx.strokeRect(vx + 0.75, 1.5, Math.min(vw, w - vx) - 1.5, oh - 3);
    ctx.beginPath();
    ctx.moveTo(sx(this.pos) + 0.5, 0);
    ctx.lineTo(sx(this.pos) + 0.5, oh);
    ctx.stroke();
    ctx.strokeStyle = c.line;
    ctx.lineWidth = 1;
    ctx.strokeRect(0.5, 0.5, w - 1, oh - 1);
  }

  // --- Zeigerbedienung ----------------------------------------------------------------------
  _local(e) {
    const r = this.main.getBoundingClientRect();
    return e.clientX - r.left;
  }

  _hit(x, touch) {
    const tol = touch ? 20 : 9;
    const te = E.endOf(this.edl, this.total);
    const xs = this.ms2x(this.edl.trim_start_ms);
    const xe = this.ms2x(te);
    // Griffstücke liegen innen an der Kante, damit sie auch am Rand der Ansicht sichtbar bleiben
    if (x >= xs - tol && x <= xs + 18) return "trimStart";
    if (x <= xe + tol && x >= xe - 18) return "trimEnd";
    if (this.sel) {
      const a = this.ms2x(this.sel.from);
      const b = this.ms2x(this.sel.to);
      if (Math.abs(x - a) <= tol) return "selFrom";
      if (Math.abs(x - b) <= tol) return "selTo";
    }
    return "bg";
  }

  _down(e) {
    if (e.button !== undefined && e.button !== 0) return;
    this.stage.focus({ preventScroll: true });
    const x = this._local(e);
    const touch = e.pointerType === "touch";
    const hit = this._hit(x, touch);
    this.main.setPointerCapture(e.pointerId);
    this.drag = { hit, x0: x, ms0: this.x2ms(x), moved: false, before: JSON.stringify(this.edl), pointer: e.pointerId };
    if (hit === "bg") this.drag.anchor = this.drag.ms0;
  }

  _move(e) {
    const x = this._local(e);
    if (!this.drag) {
      const hit = this._hit(x, e.pointerType === "touch");
      this.main.style.cursor = hit === "bg" ? "crosshair" : "ew-resize";
      return;
    }
    const d = this.drag;
    if (!d.moved && Math.abs(x - d.x0) < 4) return;
    d.moved = true;
    const ms = Math.max(0, Math.min(this.total, Math.round(this.x2ms(x))));
    if (d.hit === "trimStart") {
      this.edl = E.setTrimStart(this.edl, ms, this.total);
      this.selCut = -1;
    } else if (d.hit === "trimEnd") {
      this.edl = E.setTrimEnd(this.edl, ms, this.total);
    } else if (d.hit === "selFrom") {
      this.sel = { from: Math.min(ms, this.sel.to - E.MIN_CUT_MS), to: this.sel.to };
    } else if (d.hit === "selTo") {
      this.sel = { from: this.sel.from, to: Math.max(ms, this.sel.from + E.MIN_CUT_MS) };
    } else {
      const a = Math.round(d.anchor);
      this.sel = { from: Math.min(a, ms), to: Math.max(a, ms) };
      this.selCut = -1;
      this.markA = null;
    }
    // Rand erreicht: Ansicht mitnehmen
    if (x < 24) this.view.start -= this.visibleMs * 0.02;
    else if (x > this.w - 24) this.view.start += this.visibleMs * 0.02;
    this._clampView();
    this.draw();
    this._updateAll(false);
  }

  _up(e, cancelled = false) {
    const d = this.drag;
    this.drag = null;
    if (!d) return;
    try {
      this.main.releasePointerCapture(d.pointer);
    } catch {
      /* schon freigegeben */
    }
    if (cancelled) return;
    if (!d.moved) {
      // Klick: Abspielposition setzen; Klick in einen Schnitt wählt ihn
      const ms = Math.max(0, Math.min(this.total, this.x2ms(d.x0)));
      this.sel = null;
      this.markA = null;
      this.selCut = this.edl.cuts.findIndex((c) => ms >= c.from_ms && ms <= c.to_ms);
      this.seek(ms);
      this._updateAll();
      return;
    }
    if (d.hit === "trimStart" || d.hit === "trimEnd") {
      if (JSON.stringify(this.edl) !== d.before) {
        this.history.push(JSON.parse(d.before));
        this._changed();
      }
    }
    if (this.sel && this.sel.to - this.sel.from < E.MIN_CUT_MS) this.sel = null;
    this._updateAll();
  }

  _wheel(e) {
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      this.zoomBy(e.deltaY < 0 ? 1.25 : 0.8, this.x2ms(this._local(e)));
    } else if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
      e.preventDefault();
      this.view.start += ((e.deltaX || e.deltaY) / this.view.pps) * 1000;
      this._clampView();
      this.draw();
    }
  }

  _overDown(e) {
    this.overDrag = true;
    this.over.setPointerCapture(e.pointerId);
    this._overMove(e);
  }

  _overMove(e) {
    const r = this.over.getBoundingClientRect();
    const ms = ((e.clientX - r.left) / r.width) * this.total;
    this.scrollTo(ms);
  }

  // --- Tastatur -----------------------------------------------------------------------------
  _key(e) {
    const el = e.target;
    const inField = el instanceof HTMLElement && (el.closest("input, textarea, select, [contenteditable='true']") || (el.closest("button, summary, a") && e.key === " "));
    if (inField) return;
    if (el instanceof HTMLElement && el.closest("dialog")) return;
    if (e.ctrlKey || e.metaKey) {
      if (e.key.toLowerCase() === "z" && !e.shiftKey) {
        e.preventDefault();
        this.undo();
      }
      return;
    }
    if (e.altKey) return;
    const step = e.shiftKey ? 1000 : 5000;
    switch (e.key) {
      case " ":
        e.preventDefault();
        this.toggle();
        break;
      case "ArrowLeft":
        e.preventDefault();
        this.seek(this.pos - step);
        break;
      case "ArrowRight":
        e.preventDefault();
        this.seek(this.pos + step);
        break;
      case "Home":
        e.preventDefault();
        this.seek(0);
        this.scrollTo(0, false);
        break;
      case "End":
        e.preventDefault();
        this.seek(this.total);
        this.scrollTo(this.total, true);
        break;
      case "i":
      case "I":
        this.markIn();
        break;
      case "o":
      case "O":
        this.markOut();
        break;
      case "Delete":
      case "Backspace":
        e.preventDefault();
        if (this.sel) this.cutSelection();
        else if (this.selCut >= 0) this.removeSelectedCut();
        break;
      case "+":
      case "=":
        this.zoomBy(1.5);
        break;
      case "-":
        this.zoomBy(1 / 1.5);
        break;
      case "Escape":
        this.sel = null;
        this.markA = null;
        this.selCut = -1;
        this.draw();
        this._updateAll();
        break;
      default:
    }
  }

  // --- Wiedergabe ---------------------------------------------------------------------------
  toggle() {
    if (this.playing) this.pause();
    else this.play();
  }

  play(preview = null) {
    this.preview = preview;
    const skip = this.skip || !!preview?.skip;
    let ms = this.pos;
    if (!preview && ms >= E.endOf(this.edl, this.total) - 50) ms = this.edl.trim_start_ms;
    if (skip) {
      const target = E.skipTarget(this.edl, ms, this.total, preview?.extra || []);
      if (target === Infinity) ms = this.edl.trim_start_ms;
      else ms = target;
    }
    this.audio.currentTime = ms / 1000;
    this.pos = ms;
    const p = this.audio.play();
    if (p && p.catch) p.catch(() => (this.audioError.hidden = false));
    this.playing = true;
    this.btnPlay.replaceChildren(icon("pause"), document.createTextNode(t("Anhalten")));
    cancelAnimationFrame(this.raf);
    const loop = () => {
      if (!this.playing || this.destroyed) return;
      let ms2 = this.audio.currentTime * 1000;
      const pv = this.preview;
      if (this.skip || pv?.skip) {
        const target = E.skipTarget(this.edl, ms2, this.total, pv?.extra || []);
        if (target === Infinity) {
          this.pause();
          this.pos = E.endOf(this.edl, this.total);
          this.draw();
          return;
        }
        if (target > ms2 + 1) {
          this.audio.currentTime = target / 1000;
          ms2 = target;
        }
      }
      if (pv && ms2 >= pv.endAt) {
        this.pause();
        return;
      }
      this.pos = ms2;
      if (!this.drag) this._follow();
      this._updateReadout();
      this.draw();
      this.raf = requestAnimationFrame(loop);
    };
    this.raf = requestAnimationFrame(loop);
  }

  _follow() {
    const x = this.ms2x(this.pos);
    if (x < 0 || x > this.w * 0.92) {
      this.view.start = this.pos - this.visibleMs * 0.08;
      this._clampView();
    }
  }

  pause() {
    this.audio.pause();
    this._stopped();
  }

  _stopped() {
    if (!this.playing) return;
    this.playing = false;
    this.preview = null;
    cancelAnimationFrame(this.raf);
    this.btnPlay.replaceChildren(icon("play"), document.createTextNode(t("Abspielen")));
    this.pos = this.audio.currentTime * 1000;
    this._updateReadout();
    this.draw();
  }

  seek(ms) {
    ms = Math.max(0, Math.min(this.total, ms));
    this.pos = ms;
    this.audio.currentTime = ms / 1000;
    if (this.ms2x(ms) < 0 || this.ms2x(ms) > this.w) this.scrollTo(ms);
    this._updateReadout();
    this.draw();
  }

  hearRange(from, to, extra = []) {
    this.pause();
    const lead = 3000;
    this.pos = Math.max(0, from - lead);
    this.play({ skip: true, extra, endAt: to + lead });
  }

  hearSelection() {
    if (!this.sel) return;
    this.hearRange(this.sel.from, this.sel.to, [{ from_ms: this.sel.from, to_ms: this.sel.to }]);
  }

  // --- Bearbeiten ---------------------------------------------------------------------------
  markIn() {
    if (this.sel && this.pos < this.sel.to - E.MIN_CUT_MS) this.sel = { from: Math.round(this.pos), to: this.sel.to };
    else {
      this.sel = null;
      this.markA = Math.round(this.pos);
    }
    this.selCut = -1;
    this.draw();
    this._updateAll();
  }

  markOut() {
    if (this.markA !== null) {
      const a = Math.min(this.markA, this.pos);
      const b = Math.max(this.markA, this.pos);
      this.markA = null;
      this.sel = b - a >= E.MIN_CUT_MS ? { from: Math.round(a), to: Math.round(b) } : null;
    } else if (this.sel && this.pos > this.sel.from + E.MIN_CUT_MS) {
      this.sel = { from: this.sel.from, to: Math.round(this.pos) };
    } else {
      this._say(t("Zuerst den Beginn setzen"));
    }
    this.draw();
    this._updateAll();
  }

  cutSelection() {
    if (!this.sel) return;
    const { from, to } = this.sel;
    this.history.push(E.clone(this.edl));
    this.edl = E.addCut(this.edl, from, to, this.total);
    this.sel = null;
    this.selCut = -1;
    this._changed();
    this._say(t("Bereich {from} bis {to} herausgeschnitten", { from: fmtClock(from), to: fmtClock(to) }));
  }

  removeSelectedCut() {
    if (this.selCut < 0) return;
    const c = this.edl.cuts[this.selCut];
    this.removeCutAt(this.selCut);
    this._say(t("Schnitt {from} bis {to} entfernt", { from: fmtClock(c.from_ms), to: fmtClock(c.to_ms) }));
  }

  removeCutAt(i) {
    this.history.push(E.clone(this.edl));
    this.edl = E.removeCut(this.edl, i);
    this.selCut = -1;
    this._changed();
  }

  undo() {
    const prev = this.history.pop();
    if (!prev) return;
    this.edl = E.normalize(prev, this.total);
    this.sel = null;
    this.selCut = -1;
    this._changed();
    this._say(t("Letzte Änderung zurückgenommen"));
  }

  _changed() {
    this.draw();
    this._updateAll();
    this.o.onChange?.(E.clone(this.edl), this.isDirty());
  }

  getEdl() {
    return E.clone(this.edl);
  }

  isDirty() {
    return JSON.stringify(E.normalize(this.edl, this.total)) !== this.saved;
  }

  /** Nach dem Speichern: Server-Fassung übernehmen. */
  setSaved(edl) {
    this.edl = E.normalize(edl, this.total);
    this.saved = JSON.stringify(this.edl);
    this.history = [];
    this.sel = null;
    this.selCut = -1;
    this.draw();
    this._updateAll();
    this.o.onChange?.(E.clone(this.edl), false);
  }

  // --- Anzeige ------------------------------------------------------------------------------
  _say(msg) {
    this.lastMessage = msg;
    this.msg.textContent = msg;
  }

  _updateReadout() {
    this.readout.textContent = t("{pos} von {total}", { pos: fmtClock(this.pos, true), total: fmtClock(this.total) });
  }

  _updateAll() {
    this._updateReadout();
    const hasSel = !!this.sel && this.sel.to - this.sel.from >= E.MIN_CUT_MS;
    this.btnCut.disabled = !hasSel;
    this.btnHear.disabled = !hasSel;
    this.btnRemove.disabled = this.selCut < 0;
    this.btnUndo.disabled = this.history.length === 0;
    this.btnIn.classList.toggle("m-active", this.markA !== null);
    if (!this.msg) {
      this.msg = h("span", { class: "app-vh", role: "status" });
      this.summary.after(this.msg);
    }
    const dur = E.editedDuration(this.edl, this.total);
    const bits = [];
    if (this.sel) bits.push(t("Auswahl {from} bis {to}, {duration}", { from: fmtClock(this.sel.from, true), to: fmtClock(this.sel.to, true), duration: fmtDuration(this.sel.to - this.sel.from) || t("unter 1 s") }));
    else if (this.markA !== null) bits.push(t("Beginn bei {time}, Ende noch setzen", { time: fmtClock(this.markA, true) }));
    const info = t("Folge nach dem Schnitt: {edited}, ungeschnitten {total}", { edited: fmtClock(dur), total: fmtClock(this.total) });
    clear(this.summary);
    if (bits.length) this.summary.append(h("span", { class: "m-ed-sel" }, bits.join(". ") + ". "));
    this.summary.append(h("span", { class: "m-num" }, info));
    this._renderCuts();
  }

  _renderCuts() {
    const cuts = this.edl.cuts;
    const trims = [];
    if (this.edl.trim_start_ms > 0) trims.push(t("Anfang bis {time} getrimmt", { time: fmtClock(this.edl.trim_start_ms, true) }));
    if (this.edl.trim_end_ms != null) trims.push(t("Ende ab {time} getrimmt", { time: fmtClock(this.edl.trim_end_ms, true) }));
    clear(this.cutsList);
    this.cutsList.append(h("h3", { class: "m-h3" }, t("Schnitte ({n})", { n: cuts.length })));
    if (trims.length) this.cutsList.append(h("p", { class: "tr-small m-trims" }, trims.join(", ")));
    if (!cuts.length) {
      this.cutsList.append(h("p", { class: "tr-small" }, t("Keine Schnitte. Bereich markieren und herausschneiden.")));
      return;
    }
    const ul = h("ul", { class: "m-ed-cutlist" });
    cuts.forEach((c, i) => {
      const pause = this.pauses.some((p) => Math.abs(p.from_ms - c.from_ms) < 80 && Math.abs(p.to_ms - c.to_ms) < 80);
      ul.append(h("li", { class: i === this.selCut ? "is-sel" : "" },
        h("button", {
          type: "button", class: "m-linkbtn m-num",
          onclick: () => {
            this.selCut = i;
            this.sel = null;
            this.showRange(c.from_ms, c.to_ms);
            this.seek(c.from_ms);
            this._updateAll();
          },
        }, t("{from} bis {to}", { from: fmtClock(c.from_ms, true), to: fmtClock(c.to_ms, true) })),
        h("span", { class: "tr-small m-num" }, fmtDuration(c.to_ms - c.from_ms) || t("unter 1 s")),
        pause ? h("span", { class: "tr-tag" }, t("Pause")) : null,
        h("span", { class: "m-spacer" }),
        button(t("Übergang hören"), { onclick: () => this.hearRange(c.from_ms, c.to_ms) }),
        button(t("Entfernen"), { onclick: () => this.removeCutAt(i), ariaLabel: t("Schnitt {time} entfernen", { time: fmtClock(c.from_ms) }) })));
    });
    this.cutsList.append(ul);
  }
}
