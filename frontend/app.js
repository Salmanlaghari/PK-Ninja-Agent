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
  const div = document.createElement("div");
  div.className = "ev " + ev.type;
  const glyph = GLYPH[ev.type] ?? "•";
  const meta = ev.data && ev.data.warning
    ? `<span class="meta">⚠ ${escapeHtml(ev.data.warning)}</span>` : "";
  div.innerHTML = `<span class="glyph">${glyph}</span><span class="msg">${escapeHtml(ev.message)}${meta}</span>`;
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
    if (cfg.streaming_supported) {
      providerBadge.classList.add("live");
      providerBadge.title = `${cfg.provider} · ${cfg.model || "?"} · live streaming`;
    } else {
      providerBadge.classList.remove("live");
      providerBadge.title = `${cfg.provider} · ${cfg.model || "?"}`;
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
loadRepo();
loadProvider();
loadProviders();
loadTasks();
initMobileTabState();
setStatus("idle", "Idle");
