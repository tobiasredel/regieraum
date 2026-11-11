// Prüft die reine Schnittlogik: node tests/e2e-manage/edl.test.mjs
import assert from "node:assert/strict";
import * as E from "../../app/static/js/manage/edl.js";

const total = 600000;
let edl = E.emptyEdl();
edl = E.addCut(edl, 10000, 20000, total);
edl = E.addCut(edl, 15000, 30000, total); // überlappend: verschmilzt
assert.deepEqual(edl.cuts, [{ from_ms: 10000, to_ms: 30000 }]);
edl = E.addCut(edl, 100000, 100010, total); // zu kurz: verworfen
assert.equal(edl.cuts.length, 1);
edl = E.addCut(edl, 200000, 260000, total);
assert.equal(E.editedDuration(edl, total), total - 20000 - 60000);
edl = E.setTrimStart(edl, 5000, total);
edl = E.setTrimEnd(edl, 500000, total);
assert.equal(E.editedDuration(edl, total), 500000 - 5000 - 20000 - 60000);
assert.equal(E.skipTarget(edl, 12000, total), 30000);
assert.equal(E.skipTarget(edl, 1000, total), 5000); // vor dem Anfang
assert.equal(E.skipTarget(edl, 499999, total), 499999);
assert.equal(E.skipTarget(edl, 500000, total), Infinity);
assert.equal(E.skipTarget(edl, 50000, total, [{ from_ms: 50000, to_ms: 55000 }]), 55000);
assert.equal(E.cutAt(edl, 210000), 1);
assert.equal(E.cutAt(edl, 260000), -1);
assert.equal(E.removeCut(edl, 0).cuts.length, 1);
assert.equal(E.sessionToEpisode(edl, 300000, total), 300000 - 5000 - 20000 - 60000);
assert.equal(E.sessionToEpisode(edl, 220000, total), 200000 - 5000 - 20000);
// Trimmen darf nie mehr als eine Sekunde Rest unterschreiten
assert.equal(E.setTrimStart(E.emptyEdl(), 700000, total).trim_start_ms, total - E.MIN_KEEP_MS);
// Trim-Ende am Ende ist "kein Trim"
assert.equal(E.setTrimEnd(E.emptyEdl(), total + 5, total).trim_end_ms, null);
assert.ok(E.equal({ trim_start_ms: 0, trim_end_ms: null, cuts: [] }, E.emptyEdl()));
console.log("edl: ok");
