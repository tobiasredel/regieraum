// Identität im Browser: Host über /api/host/session, Gast über /api/join. Das Teilnehmer-Token liegt in
// localStorage; derselbe Link führt nach dem Neuladen wieder in dieselbe Identität (SPEC Abschnitte 3 und 16).

const V = new URL(import.meta.url).search;
const { api } = await import(`./util.js${V}`);

const ls = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* privater Modus */ } },
  del(k) { try { localStorage.removeItem(k); } catch { /* egal */ } },
};

export const prefs = {
  load() { try { return JSON.parse(ls.get("ps.prefs") || "{}"); } catch { return {}; } },
  save(p) { ls.set("ps.prefs", JSON.stringify({ ...prefs.load(), ...p })); },
};

export async function hostSession() {
  const old = ls.get("ps.host.token");
  const d = await api("POST", "/api/host/session", { body: old ? { participant_token: old } : {} });
  ls.set("ps.host.token", d.participant_token);
  return { token: d.participant_token, participant: d.participant };
}

/** Beitrittsseite: Link prüfen. Liefert {info, stored} oder wirft ApiError (404/410). */
export async function inviteInfo(inviteToken) {
  const stored = ls.get("ps.guest." + inviteToken);
  const info = await api("GET", `/api/join/${encodeURIComponent(inviteToken)}`);
  return { info, stored, storedName: ls.get("ps.guest.name." + inviteToken) || prefs.load().name || "" };
}

export async function joinGuest(inviteToken, name) {
  const stored = ls.get("ps.guest." + inviteToken);
  const d = await api("POST", "/api/join", { body: { invite_token: inviteToken, name, participant_token: stored || undefined } });
  ls.set("ps.guest." + inviteToken, d.participant_token);
  ls.set("ps.guest.name." + inviteToken, d.participant.name);
  return { token: d.participant_token, participant: d.participant };
}

export function forgetGuest(inviteToken) { ls.del("ps.guest." + inviteToken); }
export function forgetHost() { ls.del("ps.host.token"); }
