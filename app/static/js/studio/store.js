// Lokale Sicherung der Chunks, bevor sie hochgeladen werden. Erst OPFS (Origin Private File System), sonst
// IndexedDB, sonst nur Arbeitsspeicher (dann warnt die Oberfläche). Nach bestätigtem Upload wird gelöscht.
//
// Ablage je Spur: meta (Spur-ID, Besitzer, nächste Chunk-Nummer, Frames, abgeschlossen ja/nein, offene Uhrenpunkte)
// und Chunks mit seq, first_frame, frames.

const ROOT = "ps-rec";
const DBNAME = "ps-rec";

class OpfsStore {
  constructor(root) { this.root = root; this.kind = "opfs"; }
  static async open() {
    if (!navigator.storage || typeof navigator.storage.getDirectory !== "function") return null;
    if (typeof FileSystemFileHandle === "undefined" || typeof FileSystemFileHandle.prototype.createWritable !== "function") return null;
    const top = await navigator.storage.getDirectory();
    const root = await top.getDirectoryHandle(ROOT, { create: true });
    // Probe: Schreiben und Löschen muss gehen (privater Modus kann scheitern)
    const probe = await root.getFileHandle(".probe", { create: true });
    const w = await probe.createWritable();
    await w.write(new Uint8Array([1]));
    await w.close();
    await root.removeEntry(".probe");
    return new OpfsStore(root);
  }
  async dir(trackId, create = true) { return this.root.getDirectoryHandle(trackId, { create }); }
  async _write(dir, name, data) {
    const fh = await dir.getFileHandle(name, { create: true });
    const w = await fh.createWritable();
    await w.write(data);
    await w.close();
  }
  async putChunk(trackId, seq, firstFrame, frames, bytes) {
    const d = await this.dir(trackId);
    // erst unter Zwischennamen schreiben, dann umbenennen wäre schöner; OPFS-Writables schreiben aber ohnehin
    // erst beim close() atomar
    await this._write(d, `c-${seq}-${firstFrame}-${frames}.pcm`, bytes);
  }
  async getChunk(trackId, seq) {
    const d = await this.dir(trackId, false);
    for await (const [name, handle] of d.entries()) {
      if (name.startsWith(`c-${seq}-`)) return new Uint8Array(await (await handle.getFile()).arrayBuffer());
    }
    return null;
  }
  async delChunk(trackId, seq) {
    try {
      const d = await this.dir(trackId, false);
      for await (const name of d.keys()) if (name.startsWith(`c-${seq}-`)) await d.removeEntry(name);
    } catch { /* schon weg */ }
  }
  async listChunks(trackId) {
    const out = [];
    try {
      const d = await this.dir(trackId, false);
      for await (const name of d.keys()) {
        const m = /^c-(\d+)-(\d+)-(\d+)\.pcm$/.exec(name);
        if (m) out.push({ seq: +m[1], firstFrame: +m[2], frames: +m[3] });
      }
    } catch { /* keine Spur */ }
    return out.sort((a, b) => a.seq - b.seq);
  }
  async setMeta(trackId, meta) { await this._write(await this.dir(trackId), "meta.json", JSON.stringify(meta)); }
  async metas() {
    const out = [];
    for await (const [name, handle] of this.root.entries()) {
      if (handle.kind !== "directory") continue;
      try {
        const fh = await handle.getFileHandle("meta.json");
        out.push(JSON.parse(await (await fh.getFile()).text()));
      } catch {
        // Verzeichnis ohne meta: nur löschen, wenn es leer ist
        let empty = true;
        for await (const _ of handle.keys()) { empty = false; break; } // eslint-disable-line no-unused-vars
        if (empty) await this.root.removeEntry(name).catch(() => {});
      }
    }
    return out;
  }
  async delTrack(trackId) { await this.root.removeEntry(trackId, { recursive: true }).catch(() => {}); }
}

function idbReq(r) { return new Promise((res, rej) => { r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); }); }

class IdbStore {
  constructor(db) { this.db = db; this.kind = "indexeddb"; }
  static async open() {
    if (typeof indexedDB === "undefined") return null;
    const req = indexedDB.open(DBNAME, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      db.createObjectStore("chunks", { keyPath: ["track", "seq"] });
      db.createObjectStore("meta", { keyPath: "track_id" });
    };
    const db = await idbReq(req);
    return new IdbStore(db);
  }
  tx(store, mode = "readonly") { return this.db.transaction(store, mode).objectStore(store); }
  async putChunk(trackId, seq, firstFrame, frames, bytes) {
    await idbReq(this.tx("chunks", "readwrite").put({ track: trackId, seq, firstFrame, frames, bytes: bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) }));
  }
  async getChunk(trackId, seq) {
    const r = await idbReq(this.tx("chunks").get([trackId, seq]));
    return r ? new Uint8Array(r.bytes) : null;
  }
  async delChunk(trackId, seq) { await idbReq(this.tx("chunks", "readwrite").delete([trackId, seq])); }
  async listChunks(trackId) {
    const range = IDBKeyRange.bound([trackId, 0], [trackId, Number.MAX_SAFE_INTEGER]);
    const keys = await idbReq(this.tx("chunks").getAll(range));
    return keys.map((r) => ({ seq: r.seq, firstFrame: r.firstFrame, frames: r.frames })).sort((a, b) => a.seq - b.seq);
  }
  async setMeta(trackId, meta) { await idbReq(this.tx("meta", "readwrite").put({ ...meta, track_id: trackId })); }
  async metas() { return idbReq(this.tx("meta").getAll()); }
  async delTrack(trackId) {
    for (const c of await this.listChunks(trackId)) await this.delChunk(trackId, c.seq);
    await idbReq(this.tx("meta", "readwrite").delete(trackId));
  }
}

class MemoryStore {
  constructor() { this.kind = "memory"; this.chunks = new Map(); this.meta = new Map(); }
  async putChunk(t, seq, firstFrame, frames, bytes) { this.chunks.set(`${t}/${seq}`, { t, seq, firstFrame, frames, bytes }); }
  async getChunk(t, seq) { const c = this.chunks.get(`${t}/${seq}`); return c ? c.bytes : null; }
  async delChunk(t, seq) { this.chunks.delete(`${t}/${seq}`); }
  async listChunks(t) { return [...this.chunks.values()].filter((c) => c.t === t).map(({ seq, firstFrame, frames }) => ({ seq, firstFrame, frames })).sort((a, b) => a.seq - b.seq); }
  async setMeta(t, meta) { this.meta.set(t, { ...meta }); }
  async metas() { return [...this.meta.values()]; }
  async delTrack(t) { for (const k of [...this.chunks.keys()]) if (k.startsWith(t + "/")) this.chunks.delete(k); this.meta.delete(t); }
}

/** Bestes verfügbares Speicher-Backend. `prefer` (für Tests): "indexeddb" oder "memory". */
export async function openStore(prefer) {
  if (navigator.storage && typeof navigator.storage.persist === "function") {
    navigator.storage.persist().catch(() => {});
  }
  if (prefer !== "indexeddb" && prefer !== "memory") {
    try { const s = await OpfsStore.open(); if (s) return s; } catch { /* weiter */ }
  }
  if (prefer !== "memory") {
    try { const s = await IdbStore.open(); if (s) return s; } catch { /* weiter */ }
  }
  return new MemoryStore();
}
