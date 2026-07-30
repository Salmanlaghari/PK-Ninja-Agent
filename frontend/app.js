/* PK Ninja Agent — frontend logic. Vanilla JS, no frameworks.
   Consumes the SSE stream and renders real events only. */

const $ = (id) => document.getElementById(id);
const activity = $("activity");
const terminal = $("terminal");
const filesEl = $("files");
const diffEl = $("diff");
const statusEl = $("status");
const statusText = $("status-text");

let currentTaskId = null;
let evtSource = null;

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
};

function setStatus(state, text) {
  statusEl.className = "status-pill " + state;
  statusText.textContent = text;
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

function renderEvent(ev) {
  clearPlaceholders();
  const div = document.createElement("div");
  div.className = "ev " + ev.type;
  const glyph = GLYPH[ev.type] ?? "•";
  const meta = ev.data && ev.data.warning ? `<span class="meta">⚠ ${escapeHtml(ev.data.warning)}</span>` : "";
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

  // Status transitions.
  if (ev.type === "session_started") setStatus("running", "Running");
  if (ev.type === "completed") setStatus("ok", "Completed");
  if (ev.type === "error") setStatus("err", "Error");
  if (ev.type === "fixing") setStatus("running", "Fixing");

  // Refresh changed files + diff after editing/completed.
  if (ev.type === "editing" || ev.type === "completed" || ev.type === "info") {
    if (ev.data && (ev.data.changed || ev.data.diff !== undefined || ev.data.branch)) {
      refreshDiff();
    }
    if (ev.type === "completed") refreshDiff();
  }
}

function renderDiff(diff) {
  if (!diff) return;
  clearPlaceholders();
  // Files list
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
  // Diff text with simple coloring
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

function startStream(taskId) {
  if (evtSource) evtSource.close();
  currentTaskId = taskId;
  evtSource = new EventSource(`/api/tasks/${taskId}/stream`);
  evtSource.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      renderEvent(ev);
    } catch (err) { /* ignore keepalives */ }
  };
  evtSource.onerror = () => {
    // SSE closes after completion; that's expected.
  };
}

// ── Repository ────────────────────────────────────────────────────────────
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

// ── Start task ────────────────────────────────────────────────────────────
$("start").addEventListener("click", async () => {
  const desc = $("task").value.trim();
  if (!desc) { toast("Enter a task first.", "err"); return; }
  $("start").disabled = true;
  activity.innerHTML = "";
  terminal.innerHTML = "";
  filesEl.innerHTML = '<li class="empty-state">No changes yet.</li>';
  diffEl.innerHTML = '<span class="empty-state">No diff yet.</span>';
  setStatus("running", "Starting");
  try {
    const r = await fetch("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ description: desc }),
    });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    startStream(data.task_id);
    toast("Agent started", "ok");
  } catch (e) {
    setStatus("err", "Error");
    toast("Failed to start: " + e.message, "err");
  } finally {
    $("start").disabled = false;
  }
});

// ── Git controls ──────────────────────────────────────────────────────────
async function gitAction(url, body, okMsg) {
  if (!currentTaskId) { toast("Start a task first.", "err"); return; }
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

// ── Init ──────────────────────────────────────────────────────────────────
loadRepo();
setStatus("idle", "Idle");
