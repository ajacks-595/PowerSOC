/* Theme cycling: persists choice in localStorage and notifies listeners so
   charts/diagrams can re-theme. No dependencies; works air-gapped. */
(function () {
    "use strict";
    var KEY = "powersoc-theme";
    var THEMES = ["dark", "midnight", "radar", "terminal", "light"];
    var LABELS = { dark: "Dark", midnight: "Midnight", radar: "Radar",
                   terminal: "Terminal", light: "Light" };

    function current() {
        return document.documentElement.getAttribute("data-theme") || "dark";
    }

    function apply(theme) {
        if (THEMES.indexOf(theme) === -1) theme = "dark";
        document.documentElement.setAttribute("data-theme", theme);
        try { localStorage.setItem(KEY, theme); } catch (e) {}
        var btn = document.getElementById("theme-toggle");
        if (btn) btn.title = "Theme: " + (LABELS[theme] || theme) + " (click to change)";
        document.dispatchEvent(new CustomEvent("powersoc:theme", { detail: { theme: theme } }));
    }

    function cycle() {
        var i = THEMES.indexOf(current());
        apply(THEMES[(i + 1) % THEMES.length]);
    }

    document.addEventListener("DOMContentLoaded", function () {
        var btn = document.getElementById("theme-toggle");
        if (btn) { btn.addEventListener("click", cycle); btn.title = "Change theme"; }
    });

    window.PowerSOCTheme = { current: current, apply: apply, cycle: cycle,
                             toggle: cycle, themes: THEMES };
})();
