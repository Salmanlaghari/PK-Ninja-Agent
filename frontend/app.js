/* PK Ninja Agent v2 — frontend logic. Vanilla JS, no frameworks.
   Consumes live agent events via WebSocket (preferred) with SSE fallback,
   renders real streaming AI "thinking" tokens, supports task cancellation,
   shows provider status, and lets the user run sandboxed terminal commands.
   Every event rendered is a REAL event from the backend — nothing is faked. */

const $ = (id) => document.getElementById(id);
const activity = $("activity");
const terminal = $("terminal");
const filesEl = $("files");
const diffEl = $("diff");
const statusEl = $("status");
const statusText = $("status-text");
const providerBadge = $("provider-badge");
const providerName = $("provider-name");
const startBtn = $("start");
const cancelBtn = $("cancel");
const termInput = $("term-input");
const termRun = $("term-run");

let currentTaskId = null;
let ws = null;            // WebSocket connection (preferred)
let evtSource = null;     // SSE fallback connection
let useWS = false;        // whether we are on WebSocket transport
let wsReconnectT = null;  // reconnect timer
let wsDead = false;       // user-intentional close flag
// The current "thinking" streaming line; reused across token events until a
// non-thinking event arrives, which closes it.
let thinkingLine = null;
let thinkingLabel = null;

// Emoji glyph per event type — matches the example in the spec.
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

// Map a canonical status to a CSS status-pill class + human label.
const STATUS_MAP = {
  idle: ["idle", "Idle"],
  running: ["running", "Running"],
  success: ["success", "Completed"],
  failed: ["failed", "Failed"],
  cancelled: ["cancelled", "Cancelled"],
  // legacy aliases (backend may still emit these)
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
  // Manual terminal is only enabled once a task is running (so we know the
  // workspace + task id). It stays enabled after completion too.
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
  if (filesEl.querySelector(".empty-state")) filesEl.innerHTML = "";
  if (diffEl.querySelector(".empty-state")) diffEl.innerHTML = "";
}

// Close any open streaming "thinking" line so the next event starts fresh.
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
  thinkingLine.className = "ev thinking";
  thinkingLabel = document.createElement("div");
  thinkingLabel.className = "thinking-label typing";
  const glyph = document.createElement("span");
  glyph.className = "glyph";
  glyph.textContent = GLYPH.thinking;
  const msg = document.createElement("span");
  msg.className = "msg";
  const dots = document.createElement("span");
  dots.className = "typing-dots";
  dots.innerHTML = "<span></span><span></span><span></span>";
  thinkingLabel.appendChild(glyph);
  thinkingLabel.appendChild(msg);
  thinkingLabel.appendChild(dots);
  thinkingLine.appendChild(thinkingLabel);
  activity.appendChild(thinkingLine);
  activity.scrollTop = activity.scrollHeight;
  return thinkingLine.querySelector(".msg");
}

function appendThinkingToken(token) {
  const msgEl = ensureThinkingLine();
  msgEl.appendChild(document.createTextNode(token));
  activity.scrollTop = activity.scrollHeight;
}

function renderEvent(ev) {
  // Streamed AI tokens are handled specially: they append to one live line.
  if (ev.type === "thinking") {
    const token = (ev.data && (ev.data.token || ev.data.text)) ||
                  (typeof ev.message === "string" ? ev.message : "");
    if (token) appendThinkingToken(token);
    return;
  }
  // Any non-thinking event closes the streaming thinking line.
  closeThinkingLine();

  clearPlaceholders();
  const div = document.createElement("div");
  div.className = "ev " + ev.type;
  const glyph = GLYPH[ev.type] ?? "•";
  const meta = ev.data && ev.data.warning
    ? `<span class="meta">⚠ ${escapeHtml(ev.data.warning)}</span>` : "";
  div.innerHTML = `<span class="glyph">${glyph}</span><span class="msg">${escapeHtml(ev.message)}${meta}</span>`;
  activity.appendChild(div);
  activity.scrollTop = activity.scrollHeight;

  // Terminal panel: show real command output.
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

  // Status transitions driven by REAL events only.
  if (ev.type === "session_started") setStatus("running", "Running");
  if (ev.type === "completed") setStatus("success", "Completed");
  if (ev.type === "error") setStatus("failed", "Error");
  if (ev.type === "fixing") setStatus("running", "Fixing");
  if (ev.type === "cancelled") {
    setStatus("cancelled", "Cancelled");
    setRunningUI(false);
    closeTransport();
  }

  // Refresh changed files + diff after editing/completed.
  if (ev.type === "editing" || ev.type === "completed" || ev.type === "info") {
    if (ev.data && (ev.data.changed || ev.data.diff !== undefined || ev.data.branch)) {
      refreshDiff();
    }
    if (ev.type === "completed") {
      setRunningUI(false);
      closeTransport();
      refreshDiff();
    }
  }
}

function renderDiff(diff) {
  if (!diff) return;
  clearPlaceholders();
  if (diff.files && diff.files.length) {
    filesEl.innerHTML = diff.files.map((f) => {
      const st = f.length >= 3 ? f[0] : "M";
      const path = f.length >= 3 ? f.slice(3) : f;
      const cls = { M: "M", A: "A", D: "D", "?": "U", U: "U" }[st] || "M";
      return `<li><span class="stat ${cls}">${st === "?" ? "?" : st}</span><span class="path">${escapeHtml(path)}</span></li>`;
    }).join("");
  } else {
    filesEl.innerHTML = '<li class="empty-state">No changes yet.</li>';
  }
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

async function refreshDiff() {
  if (!currentTaskId) return;
  try {
    const r = await fetch(`/api/diff?task_id=${currentTaskId}`);
    if (r.ok) renderDiff(await r.json());
  } catch (e) { /* ignore */ }
}

// ── WebSocket transport (preferred) ─────────────────────────────────────
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

  ws.onopen = () => { /* connected — events flow on message */ };

  ws.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      renderEvent(ev);
    } catch (err) { /* ignore malformed/keepalive frames */ }
  };

  ws.onclose = () => {
    if (wsDead) return;            // intentional close — do not reconnect
    if (!currentTaskId) return;    // nothing to reconnect to
    // Reconnect once after a short delay if the agent may still be running.
    wsReconnectT = setTimeout(() => {
      if (currentTaskId && !wsDead) startWebSocket(currentTaskId);
    }, 1200);
  };

  ws.onerror = () => { /* onclose will follow */ };
}

// ── SSE fallback transport ──────────────────────────────────────────────
function startSSE(taskId) {
  useWS = false;
  if (evtSource) evtSource.close();
  evtSource = new EventSource(`/api/tasks/${taskId}/stream`);
  evtSource.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      renderEvent(ev);
    } catch (err) { /* ignore keepalives */ }
  };
  evtSource.onerror = () => {
    // SSE closes after completion; that's expected. No auto-reconnect here.
  };
}

// ── Cancel a running task ───────────────────────────────────────────────
async function cancelCurrentTask() {
  if (!currentTaskId) return;
  wsDead = true; // prevent WS auto-reconnect during teardown
  // Preferred: tell the backend over the live WebSocket.
  if (useWS && ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send(JSON.stringify({ type: "cancel" })); } catch (e) { /* fall through */ }
  }
  // Always also hit the REST cancel endpoint for reliability.
  try {
    await fetch(`/api/tasks/${currentTaskId}/cancel`, { method: "POST" });
  } catch (e) { /* ignore — WS path may have handled it */ }
  setStatus("cancelled", "Cancelling…");
}

cancelBtn.addEventListener("click", () => {
  if (!currentTaskId) return;
  cancelCurrentTask();
  toast("Cancelling…", "");
});

// ── Close all live transports ───────────────────────────────────────────
function closeTransport() {
  wsDead = true;
  if (ws) { try { ws.close(); } catch (e) {} ws = null; }
  if (wsReconnectT) { clearTimeout(wsReconnectT); wsReconnectT = null; }
  if (evtSource) { try { evtSource.close(); } catch (e) {} evtSource = null; }
}

// ── Provider config (non-secret summary) ───────────────────────────────
async function loadProvider() {
  try {
    const r = await fetch("/api/config");
    if (!r.ok) return;
    const cfg = await r.json();
    providerName.textContent = cfg.provider || "local";
    providerBadge.hidden = false;
    if (cfg.streaming_supported) {
      providerBadge.classList.add("live");
      providerBadge.title = `${cfg.provider} · ${cfg.model || "?"} · live streaming`;
    } else {
      providerBadge.classList.remove("live");
      providerBadge.title = `${cfg.provider} · ${cfg.model || "?"}`;
    }
  } catch (e) { /* backend unreachable — keep badge hidden */ }
}

// ── Repository ──────────────────────────────────────────────────────────
async function loadRepo() {
  try {
    const r = await fetch("/api/repository");
    const data = await r.json();
    const name = $("repo-name"), branch = $("repo-branch"), badge = $("repo-badge");
    if (data.configured) {
      name.textContent = data.full_name;
      name.className = "repo-name";
      branch.textContent = "default: " + data.default_branch;
      badge.hidden = false;
      badge.textContent = data.private ? "private" : "public";
      badge.className = "badge" + (data.private ? " private" : "");
    } else {
      name.textContent = "No repository configured";
      name.className = "repo-err";
      branch.textContent = "Set GITHUB_OWNER / GITHUB_REPO in .env";
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
  filesEl.innerHTML = '<li class="empty-state">No changes yet.</li>';
  diffEl.innerHTML = '<span class="empty-state">No diff yet.</span>';
  thinkingLine = null;
  setStatus("running", "Starting");
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
    // Prefer WebSocket; fall back to SSE if WS is unavailable.
    if ("WebSocket" in window && window.WebSocket) {
      startWebSocket(currentTaskId);
    } else {
      startSSE(currentTaskId);
    }
    toast("Agent started", "ok");
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
  }
}

termRun.addEventListener("click", runTerminalCommand);
termInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); runTerminalCommand(); }
});

// ── Git controls ────────────────────────────────────────────────────────
async function gitAction(url, body, okMsg) {
  if (!currentTaskId) { toast("Start a task first.", "err"); return null; }
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || JSON.stringify(data));
    toast(okMsg, "ok");
    refreshDiff();
    return data;
  } catch (e) {
    toast(e.message, "err");
    return null;
  }
}

$("btn-branch").addEventListener("click", () => {
  const branch = $("branch-name").value.trim();
  if (!branch) { toast("Enter a branch name.", "err"); return; }
  gitAction("/api/git/branch", { task_id: currentTaskId, branch }, "Branch created");
});

$("btn-commit").addEventListener("click", () => {
  const msg = prompt("Commit message:", "PK Ninja Agent changes");
  if (!msg) return;
  gitAction("/api/git/commit", { task_id: currentTaskId, message: msg }, "Committed");
});

$("btn-push").addEventListener("click", () => {
  gitAction("/api/git/push", { task_id: currentTaskId }, "Pushed");
});

$("btn-pr-prep").addEventListener("click", async () => {
  if (!currentTaskId) { toast("Start a task first.", "err"); return; }
  const data = await gitAction("/api/pr/prepare", { task_id: currentTaskId }, "PR prepared");
  if (data) {
    $("pr-out").innerHTML = `<div class="empty-state">
      <b>Base:</b> ${escapeHtml(data.base)} → <b>Head:</b> ${escapeHtml(data.head || "—")}<br>
      <b>Title:</b> ${escapeHtml(data.title)}<br>
      <b>Ready:</b> ${data.ready ? "yes" : "no (need branch + changes)"}<br>
      <details><summary style="cursor:pointer;color:var(--text-faint)">command</summary>
      <pre style="white-space:pre-wrap;word-break:break-word;margin-top:6px">${escapeHtml(data.command)}</pre></details>
    </div>`;
  }
});

$("btn-pr-create").addEventListener("click", async () => {
  if (!currentTaskId) { toast("Start a task first.", "err"); return; }
  if (!confirm("Create a real Pull Request on GitHub?")) return;
  const data = await gitAction("/api/pr/create", { task_id: currentTaskId }, "PR created!");
  if (data && data.pr_url) {
    $("pr-out").innerHTML = `<div class="empty-state">✓ PR opened: <a href="${escapeHtml(data.pr_url)}" target="_blank" style="color:var(--info)">${escapeHtml(data.pr_url)}</a></div>`;
  }
});

// ── Init ────────────────────────────────────────────────────────────────
loadRepo();
loadProvider();
setStatus("idle", "Idle");
