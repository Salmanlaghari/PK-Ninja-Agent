/* PK Ninja Agent v3 — Vanilla JS IDE Coding Workspace Controller.
   Manages Task Queue switching, Interactive File Tree, AST Symbol preview,
   Git Staging, Branch Checkouts, WebSocket SSE event streams, Live terminal,
   and fluid mobile-responsive Tab switching. */

const $ = (id) => document.getElementById(id);
const activity = $("activity");
const terminal = $("terminal");
const diffEl = $("diff");
const statusEl = $("status");
const statusText = $("status-text");
const providerBadge = $("provider-badge");
const providerName = $("provider-name");
const startBtn = $("start");
const cancelBtn = $("cancel");
const termInput = $("term-input");
const termRun = $("term-run");

/* ============================================================= */
/* v0.7.0 — Authentication & session management                  */
/*                                                               */
/* The session token (when auth is enabled) is stored in         */
/* sessionStorage and automatically attached to every fetch()    */
/* call via a transparent wrapper. When auth is disabled (the    */
/* default), this layer is a no-op and the app behaves exactly   */
/* as before (backward compatible).                              */
/* ============================================================= */

const Auth = (() => {
  const TOKEN_KEY = "pk_ninja_session";
  const USER_KEY = "pk_ninja_user";
  let authEnabled = false;
  let currentUser = null;

  function getToken() { return sessionStorage.getItem(TOKEN_KEY) || ""; }
  function setToken(t) {
    if (t) sessionStorage.setItem(TOKEN_KEY, t);
    else sessionStorage.removeItem(TOKEN_KEY);
  }
  function getUser() {
    try { return JSON.parse(sessionStorage.getItem(USER_KEY) || "null"); }
    catch { return null; }
  }
  function setUser(u) {
    if (u) sessionStorage.setItem(USER_KEY, JSON.stringify(u));
    else sessionStorage.removeItem(USER_KEY);
  }

  // Transparent fetch wrapper: attach Authorization header when we have a token.
  const _origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    const tok = getToken();
    if (tok) {
      const headers = new Headers(init.headers || {});
      if (!headers.has("Authorization")) headers.set("Authorization", "Bearer " + tok);
      init.headers = headers;
    }
    return _origFetch(input, init);
  };

  function showLogin() {
    const ov = $("login-overlay");
    if (ov) ov.hidden = false;
    document.body.classList.add("app-booting");
  }
  function hideLogin() {
    const ov = $("login-overlay");
    if (ov) ov.hidden = true;
    document.body.classList.remove("app-booting");
  }
  function showError(msg) {
    const el = $("login-error");
    if (!el) return;
    el.textContent = msg;
    el.hidden = !msg;
  }
  function renderUserMenu() {
    const menu = $("user-menu");
    if (!menu) return;
    if (!authEnabled || !currentUser) { menu.hidden = true; return; }
    menu.hidden = false;
    const nameEl = $("user-name");
    const avEl = $("user-avatar");
    if (nameEl) nameEl.textContent = currentUser.display_name || currentUser.username || "User";
    if (avEl) {
      if (currentUser.avatar_url) {
        avEl.style.backgroundImage = `url(${currentUser.avatar_url})`;
      } else {
        avEl.style.backgroundImage = "";
        avEl.style.background = "linear-gradient(135deg, var(--accent), var(--purple))";
      }
    }
  }

  async function checkStatus() {
    try {
      const r = await _origFetch("/api/auth/status");
      if (!r.ok) { authEnabled = true; return false; }
      const body = await r.json();
      authEnabled = !!body.auth_enabled;
      currentUser = body.user || getUser();
      if (currentUser) setUser(currentUser);
      return authEnabled;
    } catch (e) {
      authEnabled = false;
      return false;
    }
  }

  async function loginGuest() {
    showError("");
    const name = ($("login-guest-name") || {}).value || "Guest";
    try {
      const r = await _origFetch("/api/auth/guest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: name }),
      });
      if (!r.ok) { showError("Guest login failed (" + r.status + ")."); return false; }
      const body = await r.json();
      if (body.session) { setToken(body.session); setUser(body.user); currentUser = body.user; }
      hideLogin(); renderUserMenu(); onAuthSuccess(); return true;
    } catch (e) { showError("Network error during guest login."); return false; }
  }

  async function loginGithub() {
    showError("");
    const tok = ($("login-github-token") || {}).value || "";
    if (!tok.trim()) { showError("Please paste a GitHub token."); return false; }
    try {
      const r = await _origFetch("/api/auth/github", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ github_token: tok }),
      });
      if (!r.ok) {
        let msg = "GitHub login failed (" + r.status + ").";
        try { const b = await r.json(); if (b.detail) msg = b.detail; } catch {}
        showError(msg); return false;
      }
      const body = await r.json();
      setToken(body.session); setUser(body.user); currentUser = body.user;
      ($("login-github-token") || {}).value = "";
      hideLogin(); renderUserMenu(); onAuthSuccess(); return true;
    } catch (e) { showError("Network error during GitHub login."); return false; }
  }

  function logout() {
    // Stateless: just clear client-side state. Best-effort server call.
    _origFetch("/api/auth/logout", { method: "POST" }).catch(() => {});
    setToken(""); setUser(""); currentUser = null;
    renderUserMenu();
    if (authEnabled) showLogin();
  }

  // If a request returns 401 and auth is enabled, force re-login.
  function handle401() {
    if (!authEnabled) return;
    setToken(""); setUser(""); currentUser = null;
    renderUserMenu();
    showLogin();
  }

  // Hook called after a successful login (set by bootApp) to run the
  // deferred app initialisation.
  let _onAuthSuccess = () => {};
  function onAuthSuccess() { _onAuthSuccess(); }

  function init() {
    // Wire login buttons.
    const bGG = $("btn-login-github");
    const bG = $("btn-login-guest");
    const bLO = $("btn-logout");
    if (bGG) bGG.addEventListener("click", loginGithub);
    if (bG) bG.addEventListener("click", loginGuest);
    if (bLO) bLO.addEventListener("click", logout);
    // Enter key submits GitHub login.
    const ghInput = $("login-github-token");
    if (ghInput) ghInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") loginGithub();
    });
  }

  return {
    checkStatus, loginGuest, loginGithub, logout, init, handle401,
    showLogin, hideLogin, renderUserMenu, onAuthSuccess,
    set onAuthSuccessHook(fn) { _onAuthSuccess = fn; },
    get isEnabled() { return authEnabled; },
    get user() { return currentUser; },
  };
})();

/* ============================================================= */
/* v0.7.0 — Settings panel controller                            */
/* ============================================================= */
const Settings = (() => {
  let current = null;
  let _providers = null;      // cached provider list from /api/providers
  let _apikeyStatus = null;   // cached API key status
  let _ghStatus = null;       // cached GitHub connection status

  const fields = {
    theme: () => $("set-theme"),
    ai_provider: () => $("set-provider"),
    default_workspace: () => $("set-workspace"),
    "term-shell": () => $("set-term-shell"),
    "term-font": () => $("set-term-font"),
    "git-fetch": () => $("set-git-fetch"),
    "git-sign": () => $("set-git-sign"),
    "git-branch-prefix": () => $("set-git-branch-prefix"),
    autosave: () => $("set-autosave"),
    autocommit: () => $("set-autocommit"),
    notifications: () => $("set-notifications"),
  };

  // Per-provider "get a key" hint link.
  const PROVIDER_KEY_LINKS = {
    openai: "https://platform.openai.com/api-keys",
    anthropic: "https://console.anthropic.com/settings/keys",
    gemini: "https://aistudio.google.com/app/apikey",
    jules: "https://jules.google/",
    local: null,
  };

  function showStatus(msg, ok) {
    const el = $("settings-status");
    if (!el) return;
    el.textContent = msg;
    el.className = "settings-status " + (ok ? "ok" : "err");
    el.hidden = !msg;
    if (ok) setTimeout(() => { el.hidden = true; }, 2500);
  }

  function showApikeyStatus(msg, ok) {
    const el = $("apikey-status");
    if (!el) return;
    el.textContent = msg;
    el.className = "settings-status " + (ok ? "ok" : "err");
    el.hidden = !msg;
    if (ok) setTimeout(() => { el.hidden = true; }, 2500);
  }

  function showGhStatus(msg, ok) {
    const el = $("gh-status");
    if (!el) return;
    el.textContent = msg;
    el.className = "settings-status " + (ok ? "ok" : "err");
    el.hidden = !msg;
    if (ok) setTimeout(() => { el.hidden = true; }, 2500);
  }

  /* ── Provider cards (replaces the single dropdown) ───────────── */
  function renderProviderCards(activeName) {
    const container = $("set-provider-cards");
    if (!container) return;
    container.innerHTML = "";
    if (!_providers || !_providers.length) {
      container.innerHTML = '<div class="provider-cards-empty">No providers available.</div>';
      return;
    }
    _providers.forEach((p) => {
      const name = p.name || p;
      const card = document.createElement("div");
      card.className = "provider-card" + (name === activeName ? " selected" : "");
      card.dataset.name = name;
      const head = document.createElement("div");
      head.className = "provider-card-head";
      const nm = document.createElement("span");
      nm.className = "provider-card-name";
      nm.textContent = p.display_name || name;
      head.appendChild(nm);
      if (name === activeName) {
        const tag = document.createElement("span");
        tag.className = "pm-tag active-tag";
        tag.textContent = "active";
        head.appendChild(tag);
      }
      if (p.requires_api_key) {
        const tag = document.createElement("span");
        tag.className = "pm-tag requires-key";
        tag.textContent = "API key";
        head.appendChild(tag);
      }
      card.appendChild(head);
      if (p.description) {
        const desc = document.createElement("div");
        desc.className = "provider-card-desc";
        desc.textContent = p.description;
        card.appendChild(desc);
      }
      // Key status pill per provider.
      const ks = document.createElement("div");
      ks.className = "provider-card-keystatus";
      if (name === "local") {
        ks.innerHTML = '<span class="apikey-dot ok"></span> Built-in (no key needed)';
      } else if (p.requires_api_key) {
        const hasKey = _apikeyStatus && _apikeyStatus.has_key;
        const usingBuiltin = _apikeyStatus && _apikeyStatus.using_builtin_key && name === "jules";
        if (hasKey) {
          ks.innerHTML = '<span class="apikey-dot ok"></span> Your key is set';
        } else if (usingBuiltin) {
          ks.innerHTML = '<span class="apikey-dot builtin"></span> Using built-in default key';
        } else {
          ks.innerHTML = '<span class="apikey-dot none"></span> No key — add one below';
        }
      } else {
        ks.innerHTML = '<span class="apikey-dot ok"></span> Ready';
      }
      card.appendChild(ks);
      // "Get a key" hint link.
      const link = PROVIDER_KEY_LINKS[name];
      if (link) {
        const a = document.createElement("a");
        a.className = "apikey-link";
        a.href = link;
        a.target = "_blank";
        a.rel = "noopener";
        a.textContent = "Get a key →";
        card.appendChild(a);
      }
      card.addEventListener("click", () => selectProviderCard(name));
      container.appendChild(card);
    });
  }

  function selectProviderCard(name) {
    const hidden = $("set-provider");
    if (hidden) hidden.value = name;
    document.querySelectorAll("#set-provider-cards .provider-card").forEach((c) => {
      c.classList.toggle("selected", c.dataset.name === name);
    });
    // Update the active tag.
    document.querySelectorAll("#set-provider-cards .provider-card").forEach((c) => {
      const head = c.querySelector(".provider-card-head");
      if (!head) return;
      const existing = head.querySelector(".active-tag");
      if (c.dataset.name === name) {
        if (!existing) {
          const tag = document.createElement("span");
          tag.className = "pm-tag active-tag";
          tag.textContent = "active";
          head.appendChild(tag);
        }
      } else if (existing) {
        existing.remove();
      }
    });
    // Update the API key hint to match the newly selected provider.
    updateApikeyHint();
  }

  /* ── API key status rendering ────────────────────────────────── */
  function renderApikeyStatus(st) {
    _apikeyStatus = st || _apikeyStatus;
    const dot = $("apikey-status-dot");
    const text = $("apikey-status-text");
    const masked = $("apikey-masked");
    const removeBtn = $("apikey-remove");
    if (!dot) return;
    if (_apikeyStatus && _apikeyStatus.has_key) {
      dot.className = "apikey-dot ok";
      text.textContent = "Your API key is set";
      masked.textContent = _apikeyStatus.masked_key || "";
      if (removeBtn) removeBtn.hidden = false;
    } else if (_apikeyStatus && _apikeyStatus.using_builtin_key) {
      dot.className = "apikey-dot builtin";
      text.textContent = "Using built-in default key";
      masked.textContent = "";
      if (removeBtn) removeBtn.hidden = true;
    } else if (_apikeyStatus && _apikeyStatus.key_source && _apikeyStatus.key_source !== "none (offline local provider)") {
      dot.className = "apikey-dot ok";
      text.textContent = "Key from " + _apikeyStatus.key_source;
      masked.textContent = "";
      if (removeBtn) removeBtn.hidden = true;
    } else {
      dot.className = "apikey-dot none";
      text.textContent = "No key set — agent runs in local/offline mode";
      masked.textContent = "";
      if (removeBtn) removeBtn.hidden = true;
    }
    // Update hint based on selected provider.
    updateApikeyHint();
  }

  function updateApikeyHint() {
    const hint = $("apikey-hint");
    if (!hint) return;
    const prov = ($("set-provider") || {}).value || "local";
    if (prov === "local") {
      hint.innerHTML = "The <b>local</b> provider needs no API key — it runs built-in heuristics.";
    } else if (prov === "jules") {
      hint.innerHTML = "Jules works with a built-in default key — you can start immediately. Add your own Jules key above for higher limits. <a class='apikey-link' href='https://jules.google/' target='_blank' rel='noopener'>Get a Jules key →</a>";
    } else {
      const link = PROVIDER_KEY_LINKS[prov];
      hint.innerHTML = "The <b>" + prov + "</b> provider requires an API key." +
        (link ? " <a class='apikey-link' href='" + link + "' target='_blank' rel='noopener'>Get a key →</a>" : "");
    }
  }

  /* ── GitHub connection status rendering ──────────────────────── */
  function renderGhStatus(st) {
    _ghStatus = st || _ghStatus;
    const dot = $("gh-status-dot");
    const text = $("gh-status-text");
    const login = $("gh-login");
    const avatar = $("gh-avatar");
    const disconnect = $("gh-disconnect");
    const repoDisplay = $("gh-repo-display");
    if (!dot) return;
    if (_ghStatus && _ghStatus.connected) {
      dot.className = "apikey-dot ok";
      text.textContent = "Connected";
      if (login) login.textContent = _ghStatus.login || "";
      if (avatar && _ghStatus.avatar_url) {
        avatar.src = _ghStatus.avatar_url;
        avatar.hidden = false;
      } else if (avatar) {
        avatar.hidden = true;
      }
      if (disconnect) disconnect.hidden = false;
      if (repoDisplay) {
        repoDisplay.hidden = false;
        if (_ghStatus.repo) {
          repoDisplay.textContent = "Repo: " + _ghStatus.repo;
        } else {
          repoDisplay.textContent = "No repo bound — add owner/repo above.";
        }
      }
    } else {
      dot.className = "apikey-dot none";
      text.textContent = "Not connected";
      if (login) login.textContent = "";
      if (avatar) avatar.hidden = true;
      if (disconnect) disconnect.hidden = true;
      if (repoDisplay) {
        repoDisplay.hidden = true;
        repoDisplay.textContent = "";
      }
    }
  }

  async function loadGithubRepos() {
    const list = $("gh-repo-list");
    const msg = $("gh-status");
    if (!list) return;
    list.innerHTML = '<div class="gh-loading">Loading repositories...</div>';
    try {
      const r = await fetch("/api/github/repos");
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        list.innerHTML = '<div class="gh-error">' +
          (err.detail || "Failed to load repos. Make sure GitHub is connected.") +
          "</div>";
        return;
      }
      const data = await r.json();
      const repos = data.repos || [];
      if (repos.length === 0) {
        list.innerHTML = '<div class="gh-empty">No repositories found.</div>';
        return;
      }
      list.innerHTML = repos.map(function(r) {
        return '<div class="gh-repo-item" data-fullname="' + (r.full_name || "") + '">' +
          '<span class="gh-repo-name">' + (r.full_name || r.name || "") + "</span>" +
          (r.private ? '<span class="gh-repo-badge">private</span>' : "") +
          '<span class="gh-repo-desc">' + (r.description || "").substring(0, 60) + "</span>" +
          "</div>";
      }).join("");
    } catch (e) {
      list.innerHTML = '<div class="gh-error">Error: ' + (e.message || "unknown") + "</div>";
    }
  }

  function applyToForm(s) {
    current = s || {};
    const setVal = (id, v) => { const el = $(id); if (el) el.value = v ?? ""; };
    const setChk = (id, v) => { const el = $(id); if (el) el.checked = !!v; };
    setVal("set-theme", s.theme);
    setVal("set-provider", s.ai_provider);
    setVal("set-workspace", s.default_workspace);
    const tp = s.terminal_preferences || {};
    setVal("set-term-shell", tp.shell);
    setVal("set-term-font", tp.font_size);
    const gp = s.git_preferences || {};
    setChk("set-git-fetch", gp.auto_fetch);
    setChk("set-git-sign", gp.sign_commits);
    setVal("set-git-branch-prefix", gp.default_branch_prefix);
    setChk("set-autosave", s.auto_save);
    setChk("set-autocommit", s.auto_commit);
    setChk("set-notifications", s.notifications);
  }

  function collectFromForm() {
    const val = (id) => { const el = $(id); return el ? el.value : ""; };
    const chk = (id) => { const el = $(id); return el ? el.checked : false; };
    return {
      theme: val("set-theme"),
      ai_provider: val("set-provider"),
      default_workspace: val("set-workspace"),
      terminal_preferences: {
        shell: val("set-term-shell") || "bash",
        font_size: parseInt(val("set-term-font") || "13", 10) || 13,
        scrollback: (current && current.terminal_preferences && current.terminal_preferences.scrollback) || 5000,
      },
      git_preferences: {
        auto_fetch: chk("set-git-fetch"),
        sign_commits: chk("set-git-sign"),
        default_branch_prefix: val("set-git-branch-prefix") || "feat/",
      },
      auto_save: chk("set-autosave"),
      auto_commit: chk("set-autocommit"),
      notifications: chk("set-notifications"),
    };
  }

  async function loadProvidersList() {
    try {
      const r = await fetch("/api/providers");
      if (!r.ok) return;
      const body = await r.json();
      _providers = (body.providers && Object.values(body.providers)) || [];
      // Sort: local first, then alphabetical, active provider prominent.
      _providers.sort((a, b) => {
        if (a.name === "local") return -1;
        if (b.name === "local") return 1;
        return (a.name || "").localeCompare(b.name || "");
      });
    } catch (e) { _providers = []; }
  }

  async function loadApikeyStatus() {
    try {
      const r = await fetch("/api/settings/api-key");
      if (r.ok) { renderApikeyStatus(await r.json()); }
    } catch (e) {}
  }

  async function loadGhStatus() {
    try {
      const r = await fetch("/api/github/status");
      if (r.ok) { renderGhStatus(await r.json()); }
    } catch (e) {}
  }

  async function saveApiKey() {
    const input = $("set-apikey");
    if (!input || !input.value.trim()) {
      showApikeyStatus("Enter an API key first.", false);
      return;
    }
    try {
      const r = await fetch("/api/settings/api-key", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: input.value.trim() }),
      });
      if (!r.ok) {
        let msg = "Save failed (" + r.status + ").";
        try { const b = await r.json(); if (b.detail) msg = b.detail; } catch {}
        showApikeyStatus(msg, false);
        return;
      }
      const body = await r.json();
      input.value = "";
      renderApikeyStatus({ has_key: true, masked_key: body.masked_key, using_builtin_key: false, key_source: "your key" });
      // Refresh provider cards so the key-status pills update.
      const active = ($("set-provider") || {}).value || "local";
      renderProviderCards(active);
      showApikeyStatus("API key saved & encrypted.", true);
    } catch (e) { showApikeyStatus("Network error saving key.", false); }
  }

  async function removeApiKey() {
    try {
      const r = await fetch("/api/settings/api-key", { method: "DELETE" });
      if (!r.ok) { showApikeyStatus("Remove failed (" + r.status + ").", false); return; }
      renderApikeyStatus({ has_key: false, masked_key: "", using_builtin_key: _apikeyStatus && _apikeyStatus.using_builtin_key, key_source: "none (offline local provider)" });
      const active = ($("set-provider") || {}).value || "local";
      renderProviderCards(active);
      // Re-fetch true status (key_source may change to built-in/env).
      await loadApikeyStatus();
      renderProviderCards(active);
      showApikeyStatus("API key removed.", true);
    } catch (e) { showApikeyStatus("Network error removing key.", false); }
  }

  async function connectGithub() {
    const input = $("set-gh-token");
    const owner = ($("set-gh-owner") || {}).value || "";
    const repo = ($("set-gh-repo") || {}).value || "";
    if (!input || !input.value.trim()) {
      showGhStatus("Enter a GitHub token first.", false);
      return;
    }
    const payload = { github_token: input.value.trim() };
    if (owner.trim()) payload.owner = owner.trim();
    if (repo.trim()) payload.repo = repo.trim();
    try {
      const r = await fetch("/api/github/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        let msg = "Connect failed (" + r.status + ").";
        try { const b = await r.json(); if (b.detail) msg = b.detail; } catch {}
        showGhStatus(msg, false);
        return;
      }
      const body = await r.json();
      input.value = "";
      renderGhStatus({ connected: true, login: body.login, avatar_url: body.avatar_url, repo: body.repo });
      showGhStatus("GitHub connected as " + (body.login || "user") + ".", true);
    } catch (e) { showGhStatus("Network error connecting GitHub.", false); }
  }

  async function disconnectGithub() {
    try {
      const r = await fetch("/api/github/connect", { method: "DELETE" });
      if (!r.ok) { showGhStatus("Disconnect failed (" + r.status + ").", false); return; }
      renderGhStatus({ connected: false, login: "", avatar_url: "" });
      showGhStatus("GitHub disconnected.", true);
    } catch (e) { showGhStatus("Network error disconnecting GitHub.", false); }
  }

  async function load() {
    try {
      const r = await fetch("/api/settings");
      if (!r.ok) return;
      const s = await r.json();
      applyToForm(s);
      // Load providers list + API key status + GitHub status in parallel,
      // then render provider cards with the active selection highlighted.
      await Promise.all([loadProvidersList(), loadApikeyStatus(), loadGhStatus()]);
      renderProviderCards(s.ai_provider || "local");
      updateApikeyHint();
    } catch (e) {}
  }

  async function save() {
    try {
      const r = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(collectFromForm()),
      });
      if (!r.ok) { showStatus("Save failed (" + r.status + ").", false); return; }
      const s = await r.json();
      applyToForm(s);
      // Refresh the header provider badge so the new active provider shows.
      if (typeof loadProvider === "function") loadProvider();
      showStatus("Settings saved.", true);
      // Apply theme immediately if a theme switcher is implemented later.
    } catch (e) { showStatus("Network error saving settings.", false); }
  }

  async function reset() {
    const defaults = {
      theme: "shinobi", ai_provider: "local", default_workspace: "",
      terminal_preferences: { shell: "bash", font_size: 13, scrollback: 5000 },
      git_preferences: { auto_fetch: false, sign_commits: false, default_branch_prefix: "feat/" },
      auto_save: true, auto_commit: false, notifications: true,
    };
    try {
      const r = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(defaults),
      });
      if (r.ok) {
        applyToForm(await r.json());
        renderProviderCards("local");
        updateApikeyHint();
        showStatus("Reset to defaults.", true);
      }
    } catch (e) { showStatus("Reset failed.", false); }
  }

  function open() { const m = $("settings-modal"); if (m) m.hidden = false; load(); }
  function close() { const m = $("settings-modal"); if (m) m.hidden = true; }

  function init() {
    const bOpen = $("btn-settings");
    const bClose = $("settings-close");
    const bSave = $("settings-save");
    const bReset = $("settings-reset");
    if (bOpen) bOpen.addEventListener("click", open);
    if (bClose) bClose.addEventListener("click", close);
    if (bSave) bSave.addEventListener("click", save);
    if (bReset) bReset.addEventListener("click", reset);
    // API key + GitHub connect buttons.
    const bAkSave = $("apikey-save");
    const bAkRemove = $("apikey-remove");
    const bGhConnect = $("gh-connect");
    const bGhDisconnect = $("gh-disconnect");
    if (bAkSave) bAkSave.addEventListener("click", saveApiKey);
    if (bAkRemove) bAkRemove.addEventListener("click", removeApiKey);
    if (bGhConnect) bGhConnect.addEventListener("click", connectGithub);
    if (bGhDisconnect) bGhDisconnect.addEventListener("click", disconnectGithub);
    // v1.3.0: Load repos button.
    const bGhRepos = $("gh-load-repos");
    if (bGhRepos) bGhRepos.addEventListener("click", loadGithubRepos);
    // Enter key on the API key input saves.
    const akInput = $("set-apikey");
    if (akInput) akInput.addEventListener("keydown", (e) => { if (e.key === "Enter") saveApiKey(); });
    // Enter key on the GitHub token input connects.
    const ghInput = $("set-gh-token");
    if (ghInput) ghInput.addEventListener("keydown", (e) => { if (e.key === "Enter") connectGithub(); });
    // Click outside the box closes the modal.
    const overlay = $("settings-modal");
    if (overlay) overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close();
    });
  }

  return { init, load, save, reset, open, close, applyToForm,
           renderProviderCards, renderApikeyStatus, renderGhStatus,
           loadApikeyStatus, loadGhStatus, loadGithubRepos };
})();


/* ============================================================= */
/* v0.7.0 — Workspaces panel controller                          */
/* ============================================================= */
const Workspaces = (() => {
  function showStatus(msg, ok) {
    const el = $("ws-status");
    if (!el) return;
    el.textContent = msg;
    el.className = "settings-status " + (ok ? "ok" : "err");
    el.hidden = !msg;
    if (ok) setTimeout(() => { el.hidden = true; }, 2500);
  }

  function fmtSize(n) {
    if (!n) return "0 B";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return v.toFixed(i ? 1 : 0) + " " + u[i];
  }

  function renderItem(w) {
    const row = document.createElement("div");
    row.className = "ws-item";
    const name = document.createElement("span");
    name.className = "ws-name";
    name.textContent = w.name;
    row.appendChild(name);
    if (w.is_default) {
      const tag = document.createElement("span");
      tag.className = "ws-default-tag";
      tag.textContent = "default";
      row.appendChild(tag);
    }
    if (w.is_git_repo) {
      const gt = document.createElement("span");
      gt.className = "ws-git-tag";
      gt.textContent = "git" + (w.branch ? ":" + w.branch : "");
      row.appendChild(gt);
    }
    const meta = document.createElement("span");
    meta.className = "ws-meta";
    meta.textContent = w.file_count + " files · " + fmtSize(w.size_bytes);
    row.appendChild(meta);

    const btnSwitch = document.createElement("button");
    btnSwitch.className = "btn ghost btn-tiny";
    btnSwitch.textContent = "Switch";
    btnSwitch.addEventListener("click", () => switchWs(w.name));
    row.appendChild(btnSwitch);

    const btnRename = document.createElement("button");
    btnRename.className = "btn ghost btn-tiny";
    btnRename.textContent = "Rename";
    btnRename.addEventListener("click", () => renameWs(w.name));
    row.appendChild(btnRename);

    const btnDel = document.createElement("button");
    btnDel.className = "btn ghost btn-tiny";
    btnDel.textContent = "Delete";
    btnDel.addEventListener("click", () => deleteWs(w.name));
    row.appendChild(btnDel);
    return row;
  }

  function renderList(containerId, items) {
    const el = $(containerId);
    if (!el) return;
    el.innerHTML = "";
    if (!items || items.length === 0) {
      const empty = document.createElement("div");
      empty.className = "ws-empty";
      empty.textContent = "No workspaces yet.";
      el.appendChild(empty);
      return;
    }
    items.forEach(w => el.appendChild(renderItem(w)));
  }

  async function load() {
    try {
      const [lr, rr] = await Promise.all([
        fetch("/api/workspaces"),
        fetch("/api/workspaces/recent"),
      ]);
      if (lr.ok) {
        const body = await lr.json();
        renderList("ws-list", body.workspaces || []);
      }
      if (rr.ok) {
        const body = await rr.json();
        renderList("ws-recent-list", body.workspaces || []);
      }
    } catch (e) { showStatus("Failed to load workspaces.", false); }
  }

  async function createWs() {
    const name = $("ws-new-name").value.trim();
    const repo = $("ws-new-repo").value.trim();
    if (!name) { showStatus("Enter a workspace name.", false); return; }
    try {
      const r = await fetch("/api/workspaces", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, repo: repo || null }),
      });
      if (!r.ok) {
        const b = await r.json().catch(() => ({}));
        showStatus(b.detail || "Create failed (" + r.status + ").", false);
        return;
      }
      $("ws-new-name").value = "";
      $("ws-new-repo").value = "";
      showStatus("Workspace created.", true);
      load();
    } catch (e) { showStatus("Network error.", false); }
  }

  async function switchWs(name) {
    try {
      const r = await fetch("/api/workspaces/switch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (r.ok) { showStatus("Switched to " + name + ".", true); load(); }
      else { showStatus("Switch failed.", false); }
    } catch (e) { showStatus("Network error.", false); }
  }

  function renameWs(oldName) {
    const newName = prompt("Rename workspace to:", oldName);
    if (!newName || newName.trim() === oldName) return;
    doRename(oldName, newName.trim());
  }

  async function doRename(oldName, newName) {
    try {
      const r = await fetch("/api/workspaces", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ old_name: oldName, new_name: newName }),
      });
      if (r.ok) { showStatus("Renamed to " + newName + ".", true); load(); }
      else {
        const b = await r.json().catch(() => ({}));
        showStatus(b.detail || "Rename failed.", false);
      }
    } catch (e) { showStatus("Network error.", false); }
  }

  async function deleteWs(name) {
    if (!confirm("Delete workspace '" + name + "'? This cannot be undone.")) return;
    try {
      const r = await fetch("/api/workspaces/" + encodeURIComponent(name), { method: "DELETE" });
      if (r.ok) { showStatus("Deleted " + name + ".", true); load(); }
      else {
        const b = await r.json().catch(() => ({}));
        showStatus(b.detail || "Delete failed.", false);
      }
    } catch (e) { showStatus("Network error.", false); }
  }

  function open() { const m = $("workspaces-modal"); if (m) m.hidden = false; load(); }
  function close() { const m = $("workspaces-modal"); if (m) m.hidden = true; }

  function init() {
    const bOpen = $("btn-workspaces");
    const bClose = $("workspaces-close");
    const bCreate = $("ws-create-btn");
    if (bOpen) bOpen.addEventListener("click", open);
    if (bClose) bClose.addEventListener("click", close);
    if (bCreate) bCreate.addEventListener("click", createWs);
    const overlay = $("workspaces-modal");
    if (overlay) overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close();
    });
    const nameInput = $("ws-new-name");
    if (nameInput) nameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") createWs();
    });
  }

  return { init, load, open, close };
})();


/* ============================================================= */
/* v0.7.0 — Provider Manager panel controller                    */
/* ============================================================= */
const Providers = (() => {
  let state = null;

  function showStatus(msg, ok) {
    const el = $("pm-status");
    if (!el) return;
    el.textContent = msg;
    el.className = "settings-status " + (ok ? "ok" : "err");
    el.hidden = !msg;
    if (ok) setTimeout(() => { el.hidden = true; }, 2500);
  }

  function capYes(v) {
    return v ? '<span class="cap-yes">&#10003;</span>' : '<span class="cap-no">&#10007;</span>';
  }

  function renderCard(name, p) {
    const card = document.createElement("div");
    card.className = "pm-card" + (name === state.active ? " active" : "") + (p.enabled ? "" : " disabled");
    const head = document.createElement("div");
    head.className = "pm-card-head";
    const nm = document.createElement("span");
    nm.className = "pm-card-name";
    nm.textContent = p.display_name || name;
    head.appendChild(nm);
    const st = document.createElement("span");
    const hstatus = (p.health && p.health.status) || "unknown";
    st.className = "provider-row-status " + hstatus;
    st.textContent = hstatus;
    head.appendChild(st);
    if (name === state.active) {
      const at = document.createElement("span");
      at.className = "pm-tag";
      at.style.background = "rgba(88,166,255,0.18)";
      at.style.color = "#58a6ff";
      at.textContent = "active";
      head.appendChild(at);
    }
    card.appendChild(head);

    const tags = document.createElement("div");
    tags.className = "pm-card-tags";
    if (p.requires_api_key) {
      const t = document.createElement("span");
      t.className = "pm-tag requires-key";
      t.textContent = "requires API key";
      tags.appendChild(t);
    }
    if (!p.enabled) {
      const t = document.createElement("span");
      t.className = "pm-tag";
      t.textContent = "disabled";
      tags.appendChild(t);
    }
    const cap = p.capability || {};
    if (cap.streaming) {
      const t = document.createElement("span"); t.className = "pm-tag"; t.textContent = "streaming";
      tags.appendChild(t);
    }
    if (cap.tool_calling) {
      const t = document.createElement("span"); t.className = "pm-tag"; t.textContent = "tools";
      tags.appendChild(t);
    }
    card.appendChild(tags);

    card.addEventListener("click", () => renderDetail(name));
    return card;
  }

  function renderDetail(name) {
    const detail = $("pm-detail");
    if (!state || !state.providers || !state.providers[name]) {
      detail.className = "pm-detail ws-empty";
      detail.textContent = "Select a provider to view details.";
      return;
    }
    const p = state.providers[name];
    const cap = p.capability || {};
    const health = p.health || {};
    const isActive = (name === state.active);
    detail.className = "pm-detail";
    detail.innerHTML =
      `<div><b>${p.display_name || name}</b> <span style="opacity:0.5;font-size:12px">(${name})</span></div>` +
      `<div style="margin-top:4px;font-size:12px;opacity:0.8">${p.description || ""}</div>` +
      `<div class="cap-grid" style="margin-top:8px">` +
        `<div class="cap-item">${capYes(cap.streaming)} Streaming</div>` +
        `<div class="cap-item">${capYes(cap.tool_calling)} Tool calling</div>` +
        `<div class="cap-item">${capYes(cap.code_editing)} Code editing</div>` +
        `<div class="cap-item">CTX: ${cap.context_window != null ? cap.context_window : "?"}</div>` +
      `</div>` +
      `<div style="margin-top:6px;font-size:12px">Health: <b>${health.status || "unknown"}</b> · ` +
      `errors: ${health.error_count || 0} · successes: ${health.success_count || 0}` +
      (health.avg_response_time_ms != null ? ` · avg ${health.avg_response_time_ms}ms` : "") + `</div>` +
      `<div class="provider-actions">` +
        (isActive ? "" : `<button class="btn small" onclick="Providers.setActive('${name}')">Set Active</button>`) +
        (p.enabled ? `<button class="btn small ghost" onclick="Providers.toggle('${name}', false)">Disable</button>`
                   : `<button class="btn small" onclick="Providers.toggle('${name}', true)">Enable</button>`) +
        `<button class="btn small ghost" onclick="Providers.probe('${name}')">Health Check</button>` +
      `</div>`;
  }

  async function load() {
    try {
      const r = await fetch("/api/providers");
      if (!r.ok) { showStatus("Failed to load providers.", false); return; }
      state = await r.json();
      $("pm-active").textContent = state.active || "—";
      const hs = (state.active_health && state.active_health.status) || "unknown";
      const hp = $("pm-active-health");
      hp.textContent = hs;
      hp.className = "provider-health-pill " + hs;
      $("pm-enabled").textContent = state.enabled ? "enabled" : "disabled";
      const list = $("pm-list");
      list.innerHTML = "";
      const names = Object.keys(state.providers || {});
      if (!names.length) {
        const e = document.createElement("div");
        e.className = "ws-empty";
        e.textContent = "No providers registered.";
        list.appendChild(e);
        return;
      }
      names.forEach(n => list.appendChild(renderCard(n, state.providers[n])));
    } catch (e) { showStatus("Network error loading providers.", false); }
  }

  async function setActive(name) {
    try {
      await fetch("/api/providers/active", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      await load(); loadProvider();
      showStatus("Active provider set to " + name + ".", true);
    } catch (e) { showStatus("Failed to set active provider.", false); }
  }

  async function toggle(name, enable) {
    try {
      const url = "/api/providers/" + (enable ? "enable" : "disable");
      await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      await load();
      showStatus((enable ? "Enabled " : "Disabled ") + name + ".", true);
    } catch (e) { showStatus("Failed to toggle provider.", false); }
  }

  async function probe(name) {
    try {
      await fetch(`/api/providers/${name}/health`);
      await load(); renderDetail(name);
      showStatus("Health check complete.", true);
    } catch (e) { showStatus("Health check failed.", false); }
  }

  function open() { const m = $("providers-modal"); if (m) m.hidden = false; load(); }
  function close() { const m = $("providers-modal"); if (m) m.hidden = true; }

  function init() {
    const bOpen = $("btn-providers");
    const bClose = $("providers-close");
    const bRefresh = $("pm-refresh");
    if (bOpen) bOpen.addEventListener("click", open);
    if (bClose) bClose.addEventListener("click", close);
    if (bRefresh) bRefresh.addEventListener("click", load);
    const overlay = $("providers-modal");
    if (overlay) overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close();
    });
  }

  return { init, load, open, close, setActive, toggle, probe };
})();
window.Providers = window.Providers || {};


/* ============================================================= */
/* v0.7.0 — Dashboard panel controller                           */
/* ============================================================= */
const Dashboard = (() => {
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function renderTaskList(containerId, items) {
    const el = $(containerId);
    if (!el) return;
    el.innerHTML = "";
    if (!items || !items.length) {
      const e = document.createElement("div");
      e.className = "ws-empty";
      e.textContent = "No tasks.";
      el.appendChild(e);
      return;
    }
    items.forEach((t) => {
      const row = document.createElement("div");
      row.className = "pm-card";
      row.innerHTML =
        `<div class="pm-card-head">` +
          `<span class="pm-card-name">${esc(t.task_id)}</span>` +
          `<span class="provider-row-status ${esc(t.status)}">${esc(t.status)}</span>` +
        `</div>` +
        `<div style="font-size:12px;opacity:0.7;margin-top:3px">${esc(t.description)}</div>` +
        `<div style="font-size:11px;opacity:0.5;margin-top:2px">${esc(t.created_at)}${t.branch ? " · " + esc(t.branch) : ""}</div>`;
      el.appendChild(row);
    });
  }

  function renderHealth(containerId, components) {
    const el = $(containerId);
    if (!el) return;
    el.innerHTML = "";
    if (!components || !components.length) {
      const e = document.createElement("div");
      e.className = "ws-empty";
      e.textContent = "No health data.";
      el.appendChild(e);
      return;
    }
    components.forEach((c) => {
      const row = document.createElement("div");
      row.className = "pm-card";
      row.innerHTML =
        `<div class="pm-card-head">` +
          `<span class="pm-card-name">${esc(c.name)}</span>` +
          `<span class="provider-row-status ${esc(c.status)}">${esc(c.status)}</span>` +
        `</div>` +
        (c.detail ? `<div style="font-size:12px;opacity:0.7;margin-top:3px">${esc(c.detail)}</div>` : "");
      el.appendChild(row);
    });
  }

  async function load() {
    try {
      const r = await fetch("/api/dashboard");
      if (!r.ok) return;
      const d = await r.json();
      $("dash-agent").textContent = d.agent_status || "idle";
      $("dash-multiagent").textContent = d.multi_agent_enabled ? "on" : "off";
      renderTaskList("dash-active-tasks", d.active_tasks);
      renderTaskList("dash-recent-tasks", d.recent_tasks);
      const ws = d.workspace_status || {};
      $("dash-workspace").innerHTML =
        `Workspaces: <b>${ws.count || 0}</b>` +
        (ws.default ? ` · default: <b>${esc(ws.default)}</b>` : "") +
        (ws.names && ws.names.length ? `<br><span style="opacity:0.6;font-size:12px">${esc(ws.names.join(", "))}</span>` : "");
      const git = d.git_status || {};
      $("dash-git").innerHTML = git.configured
        ? `Repository: <b>${esc(git.full_name)}</b> · branch: <b>${esc(git.default_branch)}</b>${git.private ? " (private)" : ""}`
        : "No repository configured.";
      const prov = d.provider_status || {};
      $("dash-provider").innerHTML = prov.configured
        ? `Provider: <b>${esc(prov.provider)}</b> · model: <b>${esc(prov.model)}</b> · streaming: ${prov.streaming_supported ? "yes" : "no"}`
        : "No AI provider configured.";
      renderHealth("dash-health", d.system_health || []);
      // System status from health endpoint
      try {
        const sr = await fetch("/api/system/health");
        if (sr.ok) {
          const sh = await sr.json();
          $("dash-sysstatus").textContent = sh.status || "—";
          $("dash-version").textContent = sh.version || "0.7.0";
          renderHealth("dash-health", sh.components || []);
        }
      } catch {}
    } catch (e) {}
  }

  function open() { const m = $("dashboard-modal"); if (m) m.hidden = false; load(); }
  function close() { const m = $("dashboard-modal"); if (m) m.hidden = true; }

  function init() {
    const bOpen = $("btn-dashboard");
    const bClose = $("dashboard-close");
    const bRefresh = $("dash-refresh");
    if (bOpen) bOpen.addEventListener("click", open);
    if (bClose) bClose.addEventListener("click", close);
    if (bRefresh) bRefresh.addEventListener("click", load);
    const overlay = $("dashboard-modal");
    if (overlay) overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close();
    });
  }

  return { init, load, open, close };
})();


let currentTaskId = null;
let ws = null;            // WebSocket connection (preferred)
let evtSource = null;     // SSE fallback connection
let useWS = false;        // whether we are on WebSocket transport
let wsReconnectT = null;  // reconnect timer
let wsDead = false;       // user-intentional close flag

// The current "thinking" streaming line; reused across token events
let thinkingLine = null;
let thinkingLabel = null;

// Event type emojis
const GLYPH = {
  session_started: "✓",
  analyzing: "🧠",
  searching: "🔍",
  file_read: "📄",
  planning: "🧠",
  editing: "✏️",
  command_started: "💻",
  command_output: "›",
  command_finished: "✓",
  test_started: "🧪",
  test_finished: "✓",
  error: "✗",
  fixing: "🔧",
  completed: "📦",
  info: "•",
  thinking: "✶",
  cancelled: "✖",
};

// Canonical status classes & labels
const STATUS_MAP = {
  idle: ["idle", "Idle"],
  running: ["running", "Running"],
  success: ["success", "Completed"],
  failed: ["failed", "Failed"],
  cancelled: ["cancelled", "Cancelled"],
  pending: ["idle", "Idle"],
  completed: ["success", "Completed"],
};

function setStatus(state, text) {
  let cls = "idle", label = "Idle";
  if (STATUS_MAP[state]) { [cls, label] = STATUS_MAP[state]; }
  else if (state === "ok") { cls = "success"; label = "Completed"; }
  else if (state === "err") { cls = "failed"; label = "Failed"; }
  statusEl.className = "status-pill " + cls;
  statusText.textContent = text || label;
}

function setRunningUI(on) {
  startBtn.disabled = on;
  cancelBtn.hidden = !on;
  if (on) { termInput.disabled = false; termRun.disabled = false; }
}

function toast(msg, kind = "") {
  const t = $("toast");
  t.textContent = msg;
  t.className = (kind ? "show " + kind : "show");
  clearTimeout(t._t);
  t._t = setTimeout(() => (t.className = ""), 2600);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function clearPlaceholders() {
  if (activity.querySelector(".empty-state")) activity.innerHTML = "";
  if (terminal.querySelector(".empty")) terminal.innerHTML = "";
  if (diffEl.querySelector(".empty-state")) diffEl.innerHTML = "";
}

function closeThinkingLine() {
  if (thinkingLabel) {
    thinkingLabel.classList.remove("typing");
    thinkingLabel = null;
  }
  thinkingLine = null;
}

function ensureThinkingLine() {
  if (thinkingLine && document.body.contains(thinkingLine)) return thinkingLine;
  clearPlaceholders();
  thinkingLine = document.createElement("div");
  thinkingLine.className = "act-event evt-plan";
  thinkingLabel = document.createElement("div");
  thinkingLabel.className = "thinking-label typing";
  const icon = document.createElement("span");
  icon.className = "act-icon";
  icon.textContent = "✦";
  const body = document.createElement("div");
  body.className = "act-body";
  const title = document.createElement("div");
  title.className = "act-title";
  title.textContent = "Thinking";
  const msg = document.createElement("div");
  msg.className = "act-detail thinking-msg";
  const dots = document.createElement("span");
  dots.className = "typing-dots";
  dots.innerHTML = "<span></span><span></span><span></span>";
  body.appendChild(title);
  body.appendChild(msg);
  body.appendChild(dots);
  thinkingLabel.appendChild(icon);
  thinkingLabel.appendChild(body);
  thinkingLine.appendChild(thinkingLabel);
  activity.appendChild(thinkingLine);
  activity.scrollTop = activity.scrollHeight;
  return thinkingLine.querySelector(".thinking-msg");
}

function appendThinkingToken(token) {
  const msgEl = ensureThinkingLine();
  msgEl.appendChild(document.createTextNode(token));
  activity.scrollTop = activity.scrollHeight;
}

function updateExecutionPlanUI(planSteps) {
  if (!planSteps || !planSteps.length) return;
  const box = $("execution-plan-box");
  const stepsContainer = $("execution-steps");
  if (!box || !stepsContainer) return;

  box.hidden = false;
  stepsContainer.innerHTML = planSteps.map((step) => {
    let icon = "⏳";
    let cls = "pending";
    if (step.status === "running") { icon = "⚡"; cls = "running"; }
    else if (step.status === "success") { icon = "✅"; cls = "success"; }
    else if (step.status === "failed") { icon = "❌"; cls = "failed"; }
    else if (step.status === "retrying") { icon = "🔄"; cls = "retrying"; }
    else if (step.status === "cancelled") { icon = "✖"; cls = "cancelled"; }

    const retryLabel = step.retries > 0 ? `<span class="step-retry-badge">Retry ${step.retries}</span>` : "";

    return `
      <div class="execution-step-item ${cls}">
        <span class="step-icon">${icon}</span>
        <span class="step-desc">Step ${step.id}: ${escapeHtml(step.description)}</span>
        ${retryLabel}
      </div>
    `;
  }).join("");
}

// Map event types to card accent classes + icons (v1.4.0 SuperNinja-style cards)
const EVT_CARD_MAP = {
  session_started: { cls: "evt-ok", icon: "✓" },
  analyzing: { cls: "evt-plan", icon: "🧠" },
  searching: { cls: "evt-tool", icon: "🔍" },
  file_read: { cls: "evt-info", icon: "📄" },
  planning: { cls: "evt-plan", icon: "🧠" },
  editing: { cls: "evt-step", icon: "✏️" },
  command_started: { cls: "evt-command", icon: "💻" },
  command_output: { cls: "evt-info", icon: "›" },
  command_finished: { cls: "evt-ok", icon: "✓" },
  test_started: { cls: "evt-step", icon: "🧪" },
  test_finished: { cls: "evt-ok", icon: "✓" },
  error: { cls: "evt-err", icon: "✗" },
  fixing: { cls: "evt-warn", icon: "🔧" },
  completed: { cls: "evt-completed", icon: "📦" },
  info: { cls: "evt-info", icon: "•" },
  thinking: { cls: "evt-plan", icon: "✦" },
  cancelled: { cls: "evt-warn", icon: "✖" },
};

const EVT_TITLE_MAP = {
  session_started: "Session Started",
  analyzing: "Analyzing",
  searching: "Searching",
  file_read: "Reading File",
  planning: "Planning",
  editing: "Editing",
  command_started: "Running Command",
  command_output: "Command Output",
  command_finished: "Command Finished",
  test_started: "Running Tests",
  test_finished: "Tests Finished",
  error: "Error",
  fixing: "Fixing",
  completed: "Task Completed",
  info: "Info",
  thinking: "Thinking",
  cancelled: "Cancelled",
};

function renderEvent(ev) {
  if (ev.type === "thinking") {
    const token = (ev.data && (ev.data.token || ev.data.text)) ||
                  (typeof ev.message === "string" ? ev.message : "");
    if (token) appendThinkingToken(token);
    return;
  }
  closeThinkingLine();

  // Dynamically update structured step plan if provided in event metadata
  if (ev.data && ev.data.plan_steps) {
    updateExecutionPlanUI(ev.data.plan_steps);
  }

  clearPlaceholders();
  const cardInfo = EVT_CARD_MAP[ev.type] || { cls: "evt-info", icon: "•" };
  const title = EVT_TITLE_MAP[ev.type] || ev.type.replace(/_/g, " ");
  const warning = ev.data && ev.data.warning
    ? ` ⚠ ${escapeHtml(ev.data.warning)}` : "";
  const div = document.createElement("div");
  div.className = `act-event ${cardInfo.cls}`;
  div.innerHTML = `<span class="act-icon">${cardInfo.icon}</span><div class="act-body"><div class="act-title">${escapeHtml(title)}</div><div class="act-detail">${escapeHtml(ev.message)}${warning}</div></div>`;
  activity.appendChild(div);
  activity.scrollTop = activity.scrollHeight;

  // Render terminal commands/output
  if (ev.type === "command_started") {
    const line = document.createElement("div");
    line.className = "line cmd";
    line.textContent = "$ " + (ev.message.replace(/^\$\s*/, "") || "");
    terminal.appendChild(line);
    terminal.scrollTop = terminal.scrollHeight;
  } else if (ev.type === "command_output" && ev.data) {
    const out = (ev.data.stdout || "") + (ev.data.stderr ? "\n[stderr]\n" + ev.data.stderr : "");
    if (out.trim()) {
      const line = document.createElement("div");
      line.className = "line";
      line.textContent = out.replace(/\n$/, "");
      terminal.appendChild(line);
      terminal.scrollTop = terminal.scrollHeight;
    }
  } else if (ev.type === "command_finished" && ev.data) {
    const line = document.createElement("div");
    line.className = "line " + (ev.data.success ? "ok" : "err");
    line.textContent = `exit ${ev.data.returncode}`;
    terminal.appendChild(line);
    terminal.scrollTop = terminal.scrollHeight;
  } else if (ev.type === "test_started") {
    const line = document.createElement("div");
    line.className = "line cmd";
    line.textContent = "▶ " + (ev.data?.command || ev.message);
    terminal.appendChild(line);
    terminal.scrollTop = terminal.scrollHeight;
  } else if (ev.type === "test_finished" && ev.data) {
    const line = document.createElement("div");
    line.className = "line " + (ev.data.success ? "ok" : "err");
    line.textContent = (ev.data.success ? "✓ BUILD/TEST SUCCESSFUL" : "✗ verification failed") +
      ` (exit ${ev.data.returncode})`;
    terminal.appendChild(line);
    terminal.scrollTop = terminal.scrollHeight;
  }

  // Handle status updates
  if (ev.type === "session_started") setStatus("running", "Running");
  if (ev.type === "completed") {
    setStatus("success", "Completed");
    setRunningUI(false);
    refreshGitPanel();
    refreshExplorerTree();
    loadTasks();
  }
  if (ev.type === "error") {
    setStatus("failed", "Error");
    setRunningUI(false);
    loadTasks();
  }
  if (ev.type === "fixing") setStatus("running", "Fixing");
  if (ev.type === "cancelled") {
    setStatus("cancelled", "Cancelled");
    setRunningUI(false);
    closeTransport();
    loadTasks();
  }

  if (ev.type === "editing" || ev.type === "info") {
    refreshGitPanel();
    refreshExplorerTree();
  }
}

// ── Task switching and loading ──────────────────────────────────────────
async function loadTasks() {
  try {
    const r = await fetch("/api/tasks");
    if (!r.ok) return;
    const tasks = await r.json();
    const taskList = $("task-list");
    $("task-count").textContent = tasks.length;

    if (tasks.length === 0) {
      taskList.innerHTML = '<li class="empty-state">No tasks available.</li>';
      return;
    }

    taskList.innerHTML = tasks.map((t) => {
      const activeClass = t.task_id === currentTaskId ? "active" : "";
      const statusClass = t.status === "running" ? "pulse" : "";
      return `
        <li class="task-item ${activeClass}" onclick="selectTask('${t.task_id}')">
          <span class="task-desc">${escapeHtml(t.description)}</span>
          <span class="status-pill ${t.status} ${statusClass}">${t.status}</span>
        </li>
      `;
    }).join("");
  } catch (e) {
    console.error("loadTasks failed", e);
  }
}

async function selectTask(taskId) {
  if (currentTaskId === taskId) return;
  currentTaskId = taskId;
  toast(`Switched to Task: ${taskId.slice(0, 8)}`, "ok");

  closeTransport();
  activity.innerHTML = "";
  terminal.innerHTML = "";
  thinkingLine = null;

  // Highlight active task in sidebar
  document.querySelectorAll(".task-item").forEach(item => item.classList.remove("active"));
  loadTasks();

  // Load task detail
  try {
    const r = await fetch(`/api/tasks/${taskId}`);
    if (r.ok) {
      const task = await r.json();
      setStatus(task.status);
      setRunningUI(task.status === "running");

      // Populate events history
      if (task.events && task.events.length) {
        task.events.forEach(renderEvent);
      } else {
        activity.innerHTML = '<div class="empty-state">No activity events for this task.</div>';
      }
    }
  } catch (e) {
    console.error("selectTask failed", e);
  }

  // Refresh Git branch and Explorer Tree
  refreshGitPanel();
  refreshExplorerTree();

  // Connect to live WebSocket / SSE if running
  wsDead = false;
  if ("WebSocket" in window && window.WebSocket) {
    startWebSocket(currentTaskId);
  } else {
    startSSE(currentTaskId);
  }
}

// ── Repository Explorer Tree ─────────────────────────────────────────────
async function refreshExplorerTree() {
  if (!currentTaskId) return;
  const treeContainer = $("repo-tree");
  treeContainer.innerHTML = '<div class="empty-state">Loading workspace tree...</div>';

  try {
    const r = await fetch(`/api/tasks/${currentTaskId}/tree`);
    if (!r.ok) throw new Error("API error");
    const tree = await r.json();

    if (tree.length === 0) {
      treeContainer.innerHTML = '<div class="empty-state">No files in repository workspace.</div>';
      return;
    }

    treeContainer.innerHTML = "";
    renderTreeNode(tree, treeContainer);
  } catch (e) {
    treeContainer.innerHTML = '<div class="empty-state">Failed to load repository tree.</div>';
  }
}

function renderTreeNode(nodes, container) {
  nodes.forEach((node) => {
    const nodeEl = document.createElement("div");
    nodeEl.className = "tree-node";

    const rowEl = document.createElement("div");
    rowEl.className = "tree-row";

    const icon = node.type === "dir" ? "📁" : "📄";
    const symbolCount = (node.symbols && node.symbols.length) || 0;
    const hasSymbolsBtn = node.type === "file" && symbolCount > 0
      ? `<span class="symbols-toggle-btn" onclick="toggleSymbols(event, '${node.path}')">${symbolCount} syms</span>`
      : "";

    rowEl.innerHTML = `
      <span class="node-icon">${icon}</span>
      <span class="node-name" onclick="openFilePreview('${node.path || ""}', ${node.type === "dir"})">${escapeHtml(node.name)}</span>
      ${hasSymbolsBtn}
    `;

    nodeEl.appendChild(rowEl);

    if (node.type === "dir" && node.children) {
      const childrenContainer = document.createElement("div");
      childrenContainer.className = "tree-children";
      childrenContainer.style.display = "block"; // Open by default

      // Toggle folder visibility on click
      rowEl.querySelector(".node-name").addEventListener("click", (e) => {
        e.stopPropagation();
        const iconEl = rowEl.querySelector(".node-icon");
        if (childrenContainer.style.display === "none") {
          childrenContainer.style.display = "block";
          iconEl.textContent = "📁";
        } else {
          childrenContainer.style.display = "none";
          iconEl.textContent = "📁"; // collapsed symbol
        }
      });

      renderTreeNode(node.children, childrenContainer);
      nodeEl.appendChild(childrenContainer);
    }

    if (node.type === "file" && symbolCount > 0) {
      const symbolsDropdown = document.createElement("div");
      symbolsDropdown.className = "tree-symbols-dropdown";
      symbolsDropdown.id = `symbols-${node.path.replace(/\//g, "-")}`;
      symbolsDropdown.style.display = "none"; // Closed by default

      node.symbols.forEach((sym) => {
        const symEl = document.createElement("div");
        symEl.className = "tree-symbol-item";
        symEl.innerHTML = `
          <span class="sym-type-badge ${sym.type}">${sym.type}</span>
          <span class="sym-name">${escapeHtml(sym.name)}</span>
          <span style="color:var(--text-faint)">(L${sym.line})</span>
        `;
        symbolsDropdown.appendChild(symEl);
      });
      nodeEl.appendChild(symbolsDropdown);
    }

    container.appendChild(nodeEl);
  });
}

function toggleSymbols(event, filePath) {
  event.stopPropagation();
  const dropdown = $(`symbols-${filePath.replace(/\//g, "-")}`);
  if (dropdown) {
    dropdown.style.display = dropdown.style.display === "none" ? "block" : "none";
  }
}

async function openFilePreview(filePath, isDir) {
  if (isDir || !filePath) return;

  const modal = $("file-modal");
  const modalTitle = $("modal-title");
  const modalContent = $("modal-content");

  modalTitle.textContent = filePath;
  modalContent.textContent = "Loading file content...";
  modal.hidden = false;

  try {
    const r = await fetch(`/api/tasks/${currentTaskId}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: `cat ${filePath}` }),
    });
    if (r.ok) {
      const data = await r.json();
      modalContent.textContent = data.stdout || data.stderr || "(empty file)";
    } else {
      modalContent.textContent = "Could not preview file content.";
    }
  } catch (e) {
    modalContent.textContent = "Failed to load file.";
  }
}

$("modal-close").addEventListener("click", () => {
  $("file-modal").hidden = true;
});

// ── Git Panel Operations ──────────────────────────────────────────────────
async function refreshGitPanel() {
  if (!currentTaskId) return;

  // 1) Load branch dropdown list
  try {
    const r = await fetch(`/api/git/branches?task_id=${currentTaskId}`);
    if (r.ok) {
      const data = await r.json();
      const branchesSelect = $("git-branches");
      branchesSelect.innerHTML = data.branches.map((b) => {
        const selected = b === data.current ? "selected" : "";
        return `<option value="${escapeHtml(b)}" ${selected}>${escapeHtml(b)}</option>`;
      }).join("");
    }
  } catch (e) {}

  // 2) Load changed files list
  try {
    const r = await fetch(`/api/diff?task_id=${currentTaskId}`);
    if (r.ok) {
      const diff = await r.json();

      // Render files lists with Stage/Unstage/Discard buttons
      const gitFilesEl = $("git-files");
      if (diff.files && diff.files.length) {
        gitFilesEl.innerHTML = diff.files.map((f) => {
          const st = f.length >= 3 ? f[0] : "M";
          const path = f.length >= 3 ? f.slice(3) : f;

          // Determine if file is currently staged or unstaged to offer proper actions
          const isStaged = diff.staged && diff.staged.includes(path);
          const stageBtn = !isStaged
            ? `<button class="btn-action-git" onclick="gitStage('${path}')">Stage</button>`
            : `<button class="btn-action-git" onclick="gitUnstage('${path}')">Unstage</button>`;
          const discardBtn = `<button class="btn-action-git" onclick="gitDiscard('${path}')">Discard</button>`;

          return `
            <li class="git-file-item">
              <div class="file-info">
                <span class="file-status-letter ${st}">${st}</span>
                <span class="file-path" title="${escapeHtml(path)}">${escapeHtml(path)}</span>
              </div>
              <div class="action-buttons">
                ${stageBtn}
                ${discardBtn}
              </div>
            </li>
          `;
        }).join("");
      } else {
        gitFilesEl.innerHTML = '<li class="empty-state">No changes yet.</li>';
      }

      // Render actual diff viewer highlight
      const text = diff.staged || diff.unstaged || "";
      if (text.trim()) {
        diffEl.innerHTML = text.split("\n").map((ln) => {
          let cls = "ctx";
          if (ln.startsWith("+")) cls = "add";
          else if (ln.startsWith("-")) cls = "del";
          else if (ln.startsWith("@@")) cls = "hunk";
          return `<span class="${cls}">${escapeHtml(ln)}</span>`;
        }).join("\n");
      } else {
        diffEl.innerHTML = '<span class="empty-state">No diff yet.</span>';
      }
    }
  } catch (e) {}
}

async function gitStage(filePath) {
  if (!currentTaskId) return;
  try {
    const r = await fetch("/api/git/stage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_id: currentTaskId, path: filePath }),
    });
    if (r.ok) {
      toast(`Staged ${filePath}`, "ok");
      refreshGitPanel();
    }
  } catch (e) {}
}

async function gitUnstage(filePath) {
  if (!currentTaskId) return;
  try {
    const r = await fetch("/api/git/unstage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_id: currentTaskId, path: filePath }),
    });
    if (r.ok) {
      toast(`Unstaged ${filePath}`, "ok");
      refreshGitPanel();
    }
  } catch (e) {}
}

async function gitDiscard(filePath) {
  if (!currentTaskId) return;
  if (!confirm(`Are you sure you want to discard all changes in ${filePath}?`)) return;
  try {
    const r = await fetch("/api/git/discard", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_id: currentTaskId, path: filePath }),
    });
    if (r.ok) {
      toast(`Discarded changes in ${filePath}`, "ok");
      refreshGitPanel();
      refreshExplorerTree();
    }
  } catch (e) {}
}

// Branch Selection Change
$("git-branches").addEventListener("change", async (e) => {
  const branch = e.target.value;
  if (!branch || !currentTaskId) return;

  toast(`Switching to branch: ${branch}`, "ok");
  try {
    const r = await fetch("/api/git/checkout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_id: currentTaskId, branch, create: false }),
    });
    if (r.ok) {
      toast(`Switched branch to ${branch}`, "ok");
      refreshGitPanel();
    } else {
      toast("Branch checkout failed", "err");
    }
  } catch (err) {
    toast("Checkout failed", "err");
  }
});

// New Branch creation Button
$("btn-new-branch").addEventListener("click", async () => {
  if (!currentTaskId) return;
  const branch = prompt("Enter a name for the new branch:");
  if (!branch) return;

  toast(`Creating branch: ${branch}`, "ok");
  try {
    const r = await fetch("/api/git/checkout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_id: currentTaskId, branch, create: true }),
    });
    if (r.ok) {
      toast(`Branch ${branch} created & checkout`, "ok");
      refreshGitPanel();
    } else {
      toast("Branch creation failed", "err");
    }
  } catch (err) {
    toast("Checkout failed", "err");
  }
});

// ── Live WebSocket Reconnection and SSE ──────────────────────────────────
function wsUrl(taskId) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/api/tasks/${taskId}/ws`;
}

function startWebSocket(taskId) {
  useWS = true;
  wsDead = false;
  if (wsReconnectT) { clearTimeout(wsReconnectT); wsReconnectT = null; }
  try { ws = new WebSocket(wsUrl(taskId)); }
  catch (e) { useWS = false; startSSE(taskId); return; }

  ws.onopen = () => {};
  ws.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      renderEvent(ev);
    } catch (err) {}
  };
  ws.onclose = () => {
    if (wsDead) return;
    if (!currentTaskId) return;
    wsReconnectT = setTimeout(() => {
      if (currentTaskId && !wsDead) startWebSocket(currentTaskId);
    }, 1200);
  };
  ws.onerror = () => {};
}

function startSSE(taskId) {
  useWS = false;
  if (evtSource) evtSource.close();
  evtSource = new EventSource(`/api/tasks/${taskId}/stream`);
  evtSource.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      renderEvent(ev);
    } catch (err) {}
  };
}

async function cancelCurrentTask() {
  if (!currentTaskId) return;
  wsDead = true;
  if (useWS && ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send(JSON.stringify({ type: "cancel" })); } catch (e) {}
  }
  try {
    await fetch(`/api/tasks/${currentTaskId}/cancel`, { method: "POST" });
  } catch (e) {}
  setStatus("cancelled", "Cancelling…");
}

cancelBtn.addEventListener("click", () => {
  if (!currentTaskId) return;
  cancelCurrentTask();
  toast("Cancelling…", "");
});

function closeTransport() {
  wsDead = true;
  if (ws) { try { ws.close(); } catch (e) {} ws = null; }
  if (wsReconnectT) { clearTimeout(wsReconnectT); wsReconnectT = null; }
  if (evtSource) { try { evtSource.close(); } catch (e) {} evtSource = null; }
}

// ── Provider config ──────────────────────────────────────────────────────
async function loadProvider() {
  try {
    const r = await fetch("/api/config");
    if (!r.ok) return;
    const cfg = await r.json();
    providerName.textContent = cfg.provider || "local";
    providerBadge.hidden = false;
    // Add a credential-status indicator to the badge title.
    let credNote = "";
    if (cfg.uses_default_credential) {
      credNote = " · built-in key";
      providerBadge.classList.add("builtin");
    } else {
      providerBadge.classList.remove("builtin");
    }
    if (cfg.streaming_supported) {
      providerBadge.classList.add("live");
      providerBadge.title = `${cfg.provider} · ${cfg.model || "?"} · live streaming${credNote}`;
    } else {
      providerBadge.classList.remove("live");
      providerBadge.title = `${cfg.provider} · ${cfg.model || "?"}${credNote}`;
    }
    // Show app version in the dashboard if present.
    if (cfg.app_version) {
      const dv = $("dash-version");
      if (dv) dv.textContent = cfg.app_version;
    }
  } catch (e) {}
}

// ── Repository info ──────────────────────────────────────────────────────
async function loadRepo() {
  try {
    const r = await fetch("/api/repository");
    const data = await r.json();
    const name = $("repo-name"), badge = $("repo-badge");
    if (data.configured) {
      name.textContent = data.full_name;
      name.className = "repo-name";
      badge.hidden = false;
      badge.textContent = data.private ? "private" : "public";
      badge.className = "badge" + (data.private ? " private" : "");
    } else {
      name.textContent = "No repository configured";
      name.className = "repo-err";
      badge.hidden = true;
    }
  } catch (e) {
    $("repo-name").textContent = "Could not reach backend";
  }
}

// ── Start task ──────────────────────────────────────────────────────────
startBtn.addEventListener("click", async () => {
  const desc = $("task").value.trim();
  if (!desc) { toast("Enter a task first.", "err"); return; }
  setRunningUI(true);
  closeTransport();
  activity.innerHTML = "";
  terminal.innerHTML = "";
  thinkingLine = null;
  setStatus("running", "Starting");
  const planBox = $("execution-plan-box");
  if (planBox) planBox.hidden = true;
  try {
    const r = await fetch("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ description: desc }),
    });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    currentTaskId = data.task_id;
    wsDead = false;
    if ("WebSocket" in window && window.WebSocket) {
      startWebSocket(currentTaskId);
    } else {
      startSSE(currentTaskId);
    }
    toast("Agent started", "ok");

    // Switch to activity tab on mobile automatically
    switchMobileTab("tab-activity");

    // Reload task queue list
    setTimeout(() => {
      loadTasks();
      refreshGitPanel();
      refreshExplorerTree();
    }, 1000);
  } catch (e) {
    setStatus("failed", "Error");
    setRunningUI(false);
    toast("Failed to start: " + e.message, "err");
  }
});

// ── Manual sandboxed terminal command ───────────────────────────────────
async function runTerminalCommand() {
  const cmd = termInput.value.trim();
  if (!cmd || !currentTaskId) return;
  termRun.disabled = true;
  clearPlaceholders();
  const line = document.createElement("div");
  line.className = "line cmd";
  line.textContent = "$ " + cmd;
  terminal.appendChild(line);
  terminal.scrollTop = terminal.scrollHeight;
  try {
    const r = await fetch(`/api/tasks/${currentTaskId}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: cmd }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    if (data.stdout) {
      const o = document.createElement("div");
      o.className = "line";
      o.textContent = data.stdout.replace(/\n$/, "");
      terminal.appendChild(o);
    }
    if (data.stderr) {
      const e = document.createElement("div");
      e.className = "line err";
      e.textContent = data.stderr.replace(/\n$/, "");
      terminal.appendChild(e);
    }
    const fin = document.createElement("div");
    fin.className = "line " + (data.success ? "ok" : "err");
    fin.textContent = `exit ${data.returncode}` + (data.cancelled ? " (terminated)" : "");
    terminal.appendChild(fin);
    terminal.scrollTop = terminal.scrollHeight;
  } catch (e) {
    const el = document.createElement("div");
    el.className = "line err";
    el.textContent = "error: " + e.message;
    terminal.appendChild(el);
  } finally {
    termInput.value = "";
    termRun.disabled = false;
    termInput.focus();
    refreshGitPanel();
    refreshExplorerTree();
  }
}

termRun.addEventListener("click", runTerminalCommand);
termInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); runTerminalCommand(); }
});

// ── Git Controls buttons ──────────────────────────────────────────────────
async function gitAction(url, body, okMsg) {
  if (!currentTaskId) { toast("Start or select a task first.", "err"); return null; }
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || JSON.stringify(data));
    toast(okMsg, "ok");
    refreshGitPanel();
    refreshExplorerTree();
    return data;
  } catch (e) {
    toast(e.message, "err");
    return null;
  }
}

$("btn-commit").addEventListener("click", () => {
  const msg = $("commit-message").value.trim();
  if (!msg) { toast("Enter a commit message first.", "err"); return; }
  gitAction("/api/git/commit", { task_id: currentTaskId, message: msg }, "Committed successfully").then((res) => {
    if (res && res.success) {
      $("commit-message").value = "";
    }
  });
});

$("btn-push").addEventListener("click", () => {
  gitAction("/api/git/push", { task_id: currentTaskId }, "Pushed branch to remote");
});

$("btn-pr-prep").addEventListener("click", async () => {
  if (!currentTaskId) { toast("Start a task first.", "err"); return; }
  const data = await gitAction("/api/pr/prepare", { task_id: currentTaskId }, "PR details prepared");
  if (data) {
    $("pr-out").innerHTML = `<div class="empty-state" style="text-align:left;background:var(--bg-elev);border-radius:var(--radius-sm);padding:8px;">
      <b>Base:</b> ${escapeHtml(data.base)} &larr; <b>Head:</b> ${escapeHtml(data.head || "—")}<br>
      <b>Title:</b> ${escapeHtml(data.title)}<br>
      <b>Ready:</b> ${data.ready ? "Yes" : "No (need branch + changes)"}<br>
      <details><summary style="cursor:pointer;color:var(--text-faint);font-size:10px;">gh command</summary>
      <pre style="white-space:pre-wrap;word-break:break-word;font-size:10px;margin-top:4px;color:var(--text-dim);font-family:var(--mono);">${escapeHtml(data.command)}</pre></details>
    </div>`;
  }
});

$("btn-pr-create").addEventListener("click", async () => {
  if (!currentTaskId) { toast("Start a task first.", "err"); return; }
  if (!confirm("Create a real Pull Request on GitHub?")) return;
  const data = await gitAction("/api/pr/create", { task_id: currentTaskId }, "PR successfully created!");
  if (data && data.pr_url) {
    $("pr-out").innerHTML = `<div class="empty-state">✓ PR Opened: <a href="${escapeHtml(data.pr_url)}" target="_blank" style="color:var(--info);font-weight:600;">${escapeHtml(data.pr_url)}</a></div>`;
  }
});

$("btn-refresh-tree").addEventListener("click", (e) => {
  e.stopPropagation();
  refreshExplorerTree();
  toast("Repository tree refreshed", "ok");
});

$("btn-refresh-providers").addEventListener("click", (e) => {
  e.stopPropagation();
  loadProviders();
  toast("Providers refreshed", "ok");
});

// ── Mobile Tabs Switching ────────────────────────────────────────────────
function switchMobileTab(targetTabId) {
  // Toggle tab buttons
  document.querySelectorAll(".mobile-tabs .tab-btn").forEach((btn) => {
    if (btn.getAttribute("data-tab") === targetTabId) {
      btn.classList.add("active");
    } else {
      btn.classList.remove("active");
    }
  });

  // Determine panels to hide/show
  // Map of tab IDs to HTML element IDs
  const tabMapping = {
    "tab-task": $("tab-task"),
    "tab-activity": $("tab-activity"),
    "tab-explorer": $("panel-explorer"),
    "tab-git": $("panel-git"),
    "tab-diff": $("tab-diff"),
    "tab-terminal": $("tab-terminal") // terminal lives in right column, matches mobile button target
  };

  // We also have to handle tab content visibility inside right column
  document.querySelectorAll(".workspace-main .panel").forEach((pane) => {
    pane.classList.remove("active-tab");
  });
  document.querySelectorAll(".workspace-sidebar .panel").forEach((pane) => {
    pane.style.display = "none";
  });

  // Show only targeted element
  const activeEl = tabMapping[targetTabId];
  if (activeEl) {
    if (activeEl.parentNode.tagName === "ASIDE") {
      activeEl.style.display = "block";
    } else {
      activeEl.classList.add("active-tab");
    }
  }
}

// Register mobile tab clicks
document.querySelectorAll(".mobile-tabs .tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const tabId = btn.getAttribute("data-tab");
    switchMobileTab(tabId);
  });
});

// ── Setup default tab classes for mobile (running in mobile width initially)
function initMobileTabState() {
  if (window.innerWidth <= 960) {
    switchMobileTab("tab-task");
  } else {
    // Desktop width: restore sidebar visibility
    document.querySelectorAll(".workspace-sidebar .panel").forEach((pane) => {
      pane.style.display = "block";
    });
  }
}

window.addEventListener("resize", initMobileTabState);

// ── Global registrations for HTML onclick events ────────────────────────
// Provider management panel (v0.6.0)
let _providerState = null;

async function loadProviders() {
  const list = $("provider-list");
  const activeName = $("provider-active-name");
  const activeHealth = $("provider-active-health");
  if (!list) return;
  try {
    const r = await fetch("/api/providers");
    if (!r.ok) { list.innerHTML = '<li class="empty-state">Provider info unavailable.</li>'; return; }
    const data = await r.json();
    _providerState = data;
    activeName.textContent = data.active || "local";
    const hs = (data.active_health && data.active_health.status) || "unknown";
    activeHealth.textContent = hs;
    activeHealth.className = "provider-health-pill " + hs;
    const names = Object.keys(data.providers || {});
    if (!names.length) { list.innerHTML = '<li class="empty-state">No providers registered.</li>'; return; }
    list.innerHTML = "";
    names.forEach((name) => {
      const p = data.providers[name];
      const li = document.createElement("li");
      if (name === data.active) li.classList.add("active");
      if (!p.enabled) li.classList.add("disabled");
      const status = (p.health && p.health.status) || "unknown";
      li.innerHTML = `<span class="provider-row-name">${p.display_name || name}</span>` +
                     `<span class="provider-row-status ${status}">${status}</span>`;
      li.onclick = () => showProviderDetail(name);
      list.appendChild(li);
    });
  } catch (e) {
    list.innerHTML = '<li class="empty-state">Provider info unavailable.</li>';
  }
}

function showProviderDetail(name) {
  const detail = $("provider-detail");
  if (!_providerState || !_providerState.providers || !_providerState.providers[name]) return;
  const p = _providerState.providers[name];
  const cap = p.capability || {};
  const health = p.health || {};
  const capYes = (v) => v ? '<span class="cap-yes">&#10003;</span>' : '<span class="cap-no">&#10007;</span>';
  const isActive = (name === _providerState.active);
  detail.className = "provider-detail";
  detail.innerHTML =
    `<div><b>${p.display_name || name}</b></div>` +
    `<div style="margin-top:4px">${p.description || ""}</div>` +
    `<div class="cap-grid" style="margin-top:8px">` +
      `<div class="cap-item">${capYes(cap.streaming)} Streaming</div>` +
      `<div class="cap-item">${capYes(cap.tool_calling)} Tool calling</div>` +
      `<div class="cap-item">${capYes(cap.code_editing)} Code editing</div>` +
      `<div class="cap-item">CTX: ${cap.context_window != null ? cap.context_window : "?"}</div>` +
    `</div>` +
    `<div style="margin-top:8px">Health: <b>${health.status || "unknown"}</b> - ` +
    `errors: ${health.error_count || 0} - successes: ${health.success_count || 0}` +
    (health.avg_response_time_ms != null ? ` - avg ${health.avg_response_time_ms}ms` : "") + `</div>` +
    `<div class="provider-actions">` +
      (isActive ? "" : `<button class="btn small" onclick="setActiveProvider('${name}')">Set Active</button>`) +
      (p.enabled ? `<button class="btn small ghost" onclick="toggleProvider('${name}', false)">Disable</button>`
                 : `<button class="btn small" onclick="toggleProvider('${name}', true)">Enable</button>`) +
      `<button class="btn small ghost" onclick="probeProvider('${name}')">Health Check</button>` +
    `</div>`;
}

async function setActiveProvider(name) {
  try {
    await fetch("/api/providers/active", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    await loadProviders(); loadProvider();
  } catch (e) {}
}

async function toggleProvider(name, enable) {
  try {
    const url = "/api/providers/" + (enable ? "enable" : "disable");
    await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    await loadProviders();
  } catch (e) {}
}

async function probeProvider(name) {
  try {
    await fetch(`/api/providers/${name}/health`);
    await loadProviders(); showProviderDetail(name);
  } catch (e) {}
}

window.selectTask = selectTask;
window.gitStage = gitStage;
window.gitUnstage = gitUnstage;
window.gitDiscard = gitDiscard;
window.toggleSymbols = toggleSymbols;
window.openFilePreview = openFilePreview;
window.loadProviders = loadProviders;
window.setActiveProvider = setActiveProvider;
window.toggleProvider = toggleProvider;
window.probeProvider = probeProvider;
window.showProviderDetail = showProviderDetail;

// ── Init ────────────────────────────────────────────────────────────────
// Auth-aware boot: check whether authentication is enabled. If it is and
// we have no valid session, show the login screen and defer the rest of
// the app init until a successful login. When auth is disabled (default),
// everything boots immediately (backward compatible).
Auth.init();
Settings.init();
Workspaces.init();
Providers.init();
Dashboard.init();

// ── Loading / error helpers (v0.7.0 release prep) ────────────────────────────
const UI = (() => {
  function showLoading(msg) {
    const el = $("app-loading");
    if (el) { el.innerHTML = '<span class="app-loading-dot"></span> ' + (msg || "Loading…"); el.hidden = false; }
  }
  function hideLoading() { const el = $("app-loading"); if (el) el.hidden = true; }
  let toastTimer = null;
  function showError(msg) {
    const el = $("app-error-toast");
    if (!el) return;
    el.textContent = msg;
    el.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 5000);
  }
  return { showLoading, hideLoading, showError };
})();
window.UI = UI;

async function bootApp() {
  UI.showLoading("Connecting…");
  const enabled = await Auth.checkStatus();
  // After a deferred login completes, run the rest of the app init.
  Auth.onAuthSuccessHook = () => {
    loadRepo(); loadProvider(); loadProviders(); loadTasks();
    initMobileTabState(); setStatus("idle", "Idle"); UI.hideLoading();
  };
  if (enabled && !Auth.getToken()) {
    UI.hideLoading();
    Auth.showLogin();
    return; // app init deferred until login succeeds
  }
  Auth.renderUserMenu();
  loadRepo();
  loadProvider();
  loadProviders();
  loadTasks();
  initMobileTabState();
  setStatus("idle", "Idle");
  UI.hideLoading();
}

bootApp();
