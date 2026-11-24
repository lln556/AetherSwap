const Theme = {
  _storageKey: "color-scheme",
  _legacyKey: "theme",
  get() {
    try {
      const stored = localStorage.getItem(this._storageKey) || localStorage.getItem(this._legacyKey);
      if (stored === "light" || stored === "dark") return stored;
    } catch {
    }
    return "system";
  },
  set(mode) {
    const normalized = mode === "light" || mode === "dark" ? mode : "system";
    try {
      if (normalized === "system") {
        localStorage.removeItem(this._storageKey);
        localStorage.removeItem(this._legacyKey);
      } else {
        localStorage.setItem(this._storageKey, normalized);
        localStorage.removeItem(this._legacyKey);
      }
    } catch {
    }
    this.apply(normalized);
  },
  apply(mode) {
    const normalized = mode === "light" || mode === "dark" ? mode : "system";
    const meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) meta.content = normalized === "system" ? "light dark" : normalized;
    const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    const resolved = normalized === "system" ? (prefersDark ? "dark" : "light") : normalized;
    document.documentElement.dataset.theme = resolved;
  },
  cycle() {
    const cur = this.get();
    const next = cur === "system" ? "light" : cur === "light" ? "dark" : "system";
    this.set(next);
    toast("主题已切换", next === "system" ? "跟随系统" : next === "dark" ? "深色" : "浅色");
  },
  syncAcrossTabs() {
    window.addEventListener("storage", (e) => {
      if (e.key === this._storageKey || e.key === this._legacyKey || e.key === null) {
        this.apply(this.get());
      }
    });
  },
};
Theme.syncAcrossTabs();
