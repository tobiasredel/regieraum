// Schlichter Editor für Shownotes. Erlaubt sind nur p, br, a, ul, ol, li, strong, em (wie publish/sanitize.py).
import { button, h, safeUrl } from "./common.js";

const KEEP = new Set(["P", "BR", "A", "UL", "OL", "LI", "STRONG", "EM"]);
const RENAME = { B: "STRONG", I: "EM", DIV: "P", H1: "P", H2: "P", H3: "P", H4: "P", H5: "P", H6: "P" };
const DROP = new Set(["SCRIPT", "STYLE", "IFRAME", "OBJECT", "TEMPLATE", "NOSCRIPT"]);

function walk(node, out) {
  for (const child of [...node.childNodes]) {
    if (child.nodeType === Node.TEXT_NODE) {
      out.append(document.createTextNode(child.textContent));
    } else if (child.nodeType === Node.ELEMENT_NODE) {
      const raw = child.tagName.toUpperCase();
      if (DROP.has(raw)) continue;
      const tag = RENAME[raw] || raw;
      if (!KEEP.has(tag)) {
        walk(child, out); // unbekanntes Element: Inhalt behalten, Hülle verwerfen
        continue;
      }
      const el = document.createElement(tag.toLowerCase());
      if (tag === "A") {
        const href = child.getAttribute("href");
        const ok = href && (/^mailto:/i.test(href) || safeUrl(href));
        if (ok) {
          el.setAttribute("href", href);
        }
      }
      if (tag !== "BR") walk(child, el);
      out.append(el);
    }
  }
}

/** Bereinigt HTML auf die erlaubten Tags und liefert eine Zeichenkette. */
export function sanitizeHtml(html) {
  const doc = new DOMParser().parseFromString(`<body>${html || ""}</body>`, "text/html");
  const box = document.createElement("div");
  walk(doc.body, box);
  // Lose Textknoten und Inline-Elemente auf oberster Ebene in Absätze packen
  const blocks = new Set(["P", "UL", "OL"]);
  const final = document.createElement("div");
  let cur = null;
  for (const n of [...box.childNodes]) {
    if (n.nodeType === Node.ELEMENT_NODE && blocks.has(n.tagName)) {
      cur = null;
      final.append(n);
    } else if (n.nodeType === Node.TEXT_NODE && !n.textContent.trim() && !cur) {
      continue;
    } else {
      if (!cur) {
        cur = document.createElement("p");
        final.append(cur);
      }
      cur.append(n);
    }
  }
  for (const p of [...final.querySelectorAll("p")]) if (!p.textContent.trim() && !p.querySelector("br")) p.remove();
  return final.innerHTML;
}

function askLink(initial) {
  return new Promise((resolve) => {
    const input = h("input", { class: "tr-input", type: "url", id: "m-link-url", placeholder: "https://", value: initial || "" });
    const dlg = h("dialog", { class: "tr-modal m-dialog", "aria-labelledby": "m-link-title" });
    const done = (v) => {
      dlg.close();
      dlg.remove();
      resolve(v);
    };
    const form = h("form", { onsubmit: (e) => { e.preventDefault(); done(input.value.trim()); } },
      h("h2", { id: "m-link-title", class: "m-dialog-title" }, "Link einfügen"),
      h("div", { class: "tr-field m-field" }, h("label", { class: "tr-label", for: "m-link-url" }, "Adresse"), input),
      h("div", { class: "app-row m-dialog-actions" },
        button("Einfügen", { variant: "primary", small: false, type: "submit" }),
        button("Link entfernen", { small: false, onclick: () => done("") }),
        button("Abbrechen", { small: false, onclick: () => done(null) })));
    dlg.append(form);
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(null); });
    document.body.append(dlg);
    dlg.showModal();
    input.focus();
  });
}

export class ShownotesEditor {
  constructor({ value = "", onChange } = {}) {
    this.onChange = onChange;
    this.source = false;
    this.area = h("div", {
      class: "m-rte tr-input", contenteditable: "true", role: "textbox", "aria-multiline": "true",
      "aria-label": "Shownotes", "data-placeholder": "Shownotes eingeben",
    });
    this.text = h("textarea", { class: "tr-input m-rte-source", hidden: true, rows: 10, "aria-label": "Shownotes als HTML", spellcheck: "false" });
    this.bar = h("div", { class: "m-rte-bar", role: "toolbar", "aria-label": "Formatierung" });
    const cmd = (label, command, title) =>
      button(label, { title, onclick: () => { this.area.focus(); document.execCommand(command, false, null); this._emit(); } });
    this.btnSource = button("Quelltext", { onclick: () => this.toggleSource() });
    this.btnSource.setAttribute("aria-pressed", "false");
    this.bar.append(
      cmd("Fett", "bold", "Fett"), cmd("Kursiv", "italic", "Kursiv"),
      button("Link", { onclick: () => this.link() }),
      cmd("Liste", "insertUnorderedList", "Aufzählung"), cmd("Nummeriert", "insertOrderedList", "Nummerierte Liste"),
      this.btnSource);
    this.el = h("div", { class: "m-rte-wrap" }, this.bar, this.area, this.text);
    this.area.addEventListener("input", () => this._emit());
    this.area.addEventListener("paste", (e) => {
      e.preventDefault();
      const html = e.clipboardData.getData("text/html");
      const plain = e.clipboardData.getData("text/plain");
      const clean = html ? sanitizeHtml(html) : sanitizeHtml(plain.split(/\n{2,}/).map((t) => `<p>${t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/\n/g, "<br>")}</p>`).join(""));
      document.execCommand("insertHTML", false, clean);
    });
    this.text.addEventListener("input", () => this.onChange?.());
    this.setHtml(value);
  }

  _emit() {
    this.onChange?.();
  }

  setHtml(html) {
    const clean = sanitizeHtml(html);
    this.area.innerHTML = clean; // bereits bereinigt
    this.text.value = clean;
  }

  getHtml() {
    if (this.source) return sanitizeHtml(this.text.value);
    return sanitizeHtml(this.area.innerHTML);
  }

  toggleSource() {
    this.source = !this.source;
    if (this.source) {
      this.text.value = sanitizeHtml(this.area.innerHTML).replace(/(<\/(p|ul|ol|li)>)/g, "$1\n");
    } else {
      this.area.innerHTML = sanitizeHtml(this.text.value);
    }
    this.area.hidden = this.source;
    this.text.hidden = !this.source;
    this.btnSource.setAttribute("aria-pressed", String(this.source));
    this.bar.querySelectorAll("button").forEach((b) => { if (b !== this.btnSource) b.disabled = this.source; });
  }

  async link() {
    const sel = window.getSelection();
    const range = sel.rangeCount ? sel.getRangeAt(0).cloneRange() : null;
    const existing = range && range.startContainer.parentElement?.closest("a");
    const url = await askLink(existing?.getAttribute("href") || "");
    this.area.focus();
    if (range) {
      sel.removeAllRanges();
      sel.addRange(range);
    }
    if (url === null) return;
    if (url === "") document.execCommand("unlink");
    else if (safeUrl(url) || /^mailto:/i.test(url)) {
      if (sel.isCollapsed) document.execCommand("insertHTML", false, `<a href="${url.replace(/"/g, "&quot;")}">${url.replace(/</g, "&lt;")}</a>`);
      else document.execCommand("createLink", false, url);
    }
    this._emit();
  }
}
