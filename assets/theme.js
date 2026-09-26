(() => {
  "use strict";

  const key = "dundee-property-watch-theme";
  const root = document.documentElement;
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  let choice = null;
  try {
    const saved = localStorage.getItem(key);
    if (saved === "light" || saved === "dark") choice = saved;
  } catch (_) {
    // The toggle still works when the browser blocks persistent storage.
  }

  function apply(theme) {
    root.dataset.theme = theme;
    const button = document.getElementById("theme-toggle");
    if (button) button.setAttribute("aria-pressed", String(theme === "dark"));
    document.querySelector('meta[name="theme-color"]').content =
      theme === "dark" ? "#101a17" : "#147d59";
  }

  // This script runs before the stylesheets to avoid a flash of the wrong theme.
  apply(choice || (system.matches ? "dark" : "light"));

  function setup() {
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    button.hidden = false;
    button.setAttribute("aria-pressed", String(root.dataset.theme === "dark"));
    button.addEventListener("click", () => {
      choice = root.dataset.theme === "dark" ? "light" : "dark";
      apply(choice);
      try {
        localStorage.setItem(key, choice);
      } catch (_) {
        // Keep this visit usable even if saving the preference is unavailable.
      }
    });
  }

  system.addEventListener("change", () => {
    if (!choice) apply(system.matches ? "dark" : "light");
  });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setup, { once: true });
  } else {
    setup();
  }
})();
