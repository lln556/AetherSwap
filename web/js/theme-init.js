(() => {
  try {
    const saved = localStorage.getItem("color-scheme") || localStorage.getItem("theme");
    const valid = saved === "light" || saved === "dark" ? saved : "light dark";
    const meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) meta.content = valid;
    if (valid === "light" || valid === "dark") {
      document.documentElement.dataset.theme = valid;
    } else if (window.matchMedia) {
      document.documentElement.dataset.theme = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
  } catch {
  }
})();
