// Standard-Thema von Regieraum. Schnittstelle siehe docs/THEME.md.
// App-Basis: Menü-Knopf für die Seitenleiste auf schmalen Bildschirmen und Schatten der Kopfleiste beim Scrollen.
// Einbinden mit <script src="design/app-basis.js" defer></script>. Ohne Skript bleibt alles bedienbar außer dem Menü.
(() => {
  const knopf = document.querySelector(".tr-menu-button");
  const rahmen = document.querySelector(".tr-shell");
  if (knopf && rahmen) {
    knopf.setAttribute("aria-expanded", "false");
    knopf.addEventListener("click", () => {
      const offen = rahmen.classList.toggle("nav-offen");
      knopf.setAttribute("aria-expanded", String(offen));
    });
  }
  const kopf = document.querySelector(".tr-header");
  if (kopf) {
    const pruefe = () => kopf.classList.toggle("is-scrolled", window.scrollY > 0);
    window.addEventListener("scroll", pruefe, { passive: true });
    pruefe();
  }
})();
