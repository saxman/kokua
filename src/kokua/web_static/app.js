const log = document.getElementById("log");
const form = document.getElementById("form");
const input = document.getElementById("msg");
const sendBtn = document.getElementById("send");
const stopBtn = document.getElementById("stop");
const planToggle = document.getElementById("plan-toggle");
const thinkingLevel = document.getElementById("thinking-level");
const attachBtn = document.getElementById("attach-btn");
const imageInput = document.getElementById("image-input");
const attachPreviews = document.getElementById("attach-previews");
const convList = document.getElementById("conv-list");
const conversationsToggle = document.getElementById("conversations-toggle");
const conversationsCount = document.getElementById("conversations-count");
const newConvBtn = document.getElementById("new-conv");
const tasksSection = document.getElementById("tasks");
const tasksToggle = document.getElementById("tasks-toggle");
const tasksRefresh = document.getElementById("tasks-refresh");
const tasksCount = document.getElementById("tasks-count");
const taskList = document.getElementById("task-list");
const notifications = document.getElementById("notifications");
const convHeading = document.getElementById("conv-heading");

// A background turn finished while the user was viewing a different conversation ("notification"
// frame): show a dismissible banner rather than stealing the current view.
function showNotification(text) {
  const el = document.createElement("div");
  el.className = "notification-banner";
  const span = document.createElement("span");
  span.textContent = text;
  const dismiss = document.createElement("button");
  dismiss.type = "button";
  dismiss.textContent = "Dismiss";
  dismiss.addEventListener("click", () => el.remove());
  el.appendChild(span);
  el.appendChild(dismiss);
  notifications.appendChild(el);
}

// The inline "working" indicator: a dim row saying a turn is under way, pinned to the foot of the
// transcript for the turn's whole life, with the turn's output growing above it. It carries a spinner
// and the seconds since the turn started, which is the half that says a slow turn is not a wedged one.
//
// Pinned, rather than cleared by the first thing that renders. That was the first shape of this, and
// measuring it settled the question: on a local endpoint the first token lands about 25ms after the
// send, so a row that content displaces is on screen for a frame or two and is never seen at all. What
// the row answers is "is a turn running", which stays true until the turn ends, so that is its life.
//
// The pinning is what `appendToLog` is for: content is inserted *ahead* of the row rather than appended
// after it, so the row stays last without ever being moved.
// The heavy braille cycle rather than the light one: at the row's size a mostly-empty cell reads as a
// speck of dust, while these are a solid mark that visibly turns.
const SPINNER_FRAMES = ["\u28fe", "\u28fd", "\u28fb", "\u28bf", "\u287f", "\u28df", "\u28ef", "\u28f7"];
const SPINNER_MS = 120;
const reduceMotion = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;

let workingElement = null;
let workingTicker = null;    // spinner + elapsed interval, running only while the row is up
let workingStartedAt = 0;    // when the turn began, which is what the count counts from
let spinnerFrame = 0;

// Raise the indicator for a turn that has already been running `elapsedMs` (0 for one this page just
// sent, the server's own figure for one it is switching into), or take it down with null. A duration
// rather than a flag beside one, matching `ChannelUI.show_working`, so "idle for 12s" cannot be said.
function setWorking(elapsedMs) {
  if (elapsedMs === null) {
    removeWorking();
    return;
  }
  if (workingElement) return;  // already up for this turn: leave its clock alone
  workingStartedAt = Date.now() - elapsedMs;
  workingElement = document.createElement("div");
  workingElement.className = "working-inline";
  workingElement.setAttribute("role", "status");
  const spinner = document.createElement("span");
  spinner.className = "working-spinner";
  spinner.setAttribute("aria-hidden", "true");
  const label = document.createElement("span");
  label.textContent = "Working\u2026";
  const elapsed = document.createElement("span");
  elapsed.className = "working-elapsed";
  // Left unannounced: inside a role="status" row, a counter rewritten every second would talk over
  // everything else a screen reader has to say, and "Working…" is the part carrying the meaning.
  elapsed.setAttribute("aria-hidden", "true");
  workingElement.appendChild(spinner);
  workingElement.appendChild(label);
  workingElement.appendChild(elapsed);
  log.appendChild(workingElement);  // last, and appendToLog is what keeps it there
  tickWorking(spinner, elapsed);  // paint now rather than leaving the row blank for one interval
  workingTicker = setInterval(() => tickWorking(spinner, elapsed), SPINNER_MS);
  autoscroll();
}

function removeWorking() {
  if (workingTicker !== null) clearInterval(workingTicker);
  workingTicker = null;
  if (workingElement) workingElement.remove();
  workingElement = null;
}

// Advance the glyph and the count. Under prefers-reduced-motion the glyph holds still and the seconds
// are the only thing moving, which is the half that actually says the turn is not wedged.
function tickWorking(spinner, elapsed) {
  if (!(reduceMotion && reduceMotion.matches)) {
    spinner.textContent = SPINNER_FRAMES[spinnerFrame];
    spinnerFrame = (spinnerFrame + 1) % SPINNER_FRAMES.length;
  } else if (!spinner.textContent) {
    spinner.textContent = SPINNER_FRAMES[0];
  }
  const text = Math.floor((Date.now() - workingStartedAt) / 1000) + "s";
  if (elapsed.textContent !== text) elapsed.textContent = text;
}

// The single way anything reaches the transcript, so a renderer added later cannot leave the working
// indicator stranded above the content it was meant to sit under. While a turn is running the row is
// the log's last element, so a top-level block goes in ahead of it; `parent` is a sub-agent card's
// body for a nested block, which is inside a block already placed and so just appends.
function appendToLog(el, parent) {
  if (parent) parent.appendChild(el);
  else if (workingElement) log.insertBefore(el, workingElement);
  else log.appendChild(el);
}

// The last block rendered in the transcript, looking past the working indicator. Throughout a turn the
// indicator *is* the log's final element, and a caller asking whether anything has landed since the
// last token means content, not the row saying a turn is running. Without this, that caller closes the
// streaming bubble on every token and the answer arrives one bubble per token.
function lastContentElement() {
  const last = log.lastElementChild;
  return last === workingElement ? last.previousElementSibling : last;
}

// A conversation's age for the sidebar. Coarse on purpose: the sidebar answers "which of these did I
// touch recently", not "when exactly", which the row's own transcript already says. A future
// updated_at (clock skew) yields a negative age and reads as "just now", which is the right answer
// for a row that was only just touched.
function relativeTime(iso) {
  if (!iso) return "";
  const then = new Date(iso);
  if (isNaN(then.getTime())) return "";
  const seconds = Math.floor((Date.now() - then.getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return Math.floor(seconds / 60) + "m";
  if (seconds < 86400) return Math.floor(seconds / 3600) + "h";
  if (seconds < 172800) return "yesterday";
  return then.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// The two sidebar frames arrive independently and each needs the other to render: a task row lists
// its conversations, and the chat list excludes them. Both are kept here and both lists re-render
// whenever either frame arrives.
let lastConversations = [];
let lastTasks = [];

function selectConversation(id) {
  if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "select", id }));
}

// A conversation belongs to a task if that task minted it (`task_id`, stamped at mint time). A
// conversation minted before task_id was recorded carries no link and stays in the chat list.
function conversationsOfTask(task) {
  return lastConversations.filter((conv) => conv.task_id && conv.task_id === task.name);
}

// Whether a firing of this task is happening right now, which is what the row offers Stop for. Read
// off the task's conversations rather than from the task frame: the running state changes while the
// registry does not, and the conversation list is the frame the core already pushes when it does.
function taskIsRunning(task) {
  return conversationsOfTask(task).some((conv) => conv.running);
}

// Ids nested under a task that is *currently in the list*. Deleting a task therefore returns its
// conversations to the chat list instead of hiding them: excluding every conversation with a task_id
// would leave an orphan unreachable from the sidebar entirely.
function nestedConversationIds() {
  const ids = new Set();
  for (const task of lastTasks) {
    for (const conv of conversationsOfTask(task)) ids.add(conv.id);
  }
  return ids;
}

// The conversation whose title is being edited in place, or null. Held because a rename is the one
// sidebar interaction with state the server does not have: until the input is committed, the typed
// title exists nowhere else, so a `conversations` frame arriving mid-edit must not redraw over it.
// `renderConversations` defers instead, and `endRename` runs the render it skipped.
let renamingId = null;
let renderDeferred = false;

// Conversations whose title the model is currently writing. Cleared wholesale by the next
// `conversations` frame, which is the answer: `Assistant._rewrite_title` pushes the list however the
// call ends, a decline included, so no request can leave a row saying "naming" forever.
const awaitingTitle = new Set();

function endRename(rerender) {
  renamingId = null;
  if (rerender || renderDeferred) {
    renderDeferred = false;
    renderConversations();
  }
}

// Swap a row's title for an input, and commit or abandon what is typed into it. Enter and blur both
// commit, Escape abandons: the three behaviours a rename-in-place has everywhere else. The editor also
// carries the ask-the-model button, rather than the row: five controls do not fit in a 240px row, and
// at that width the title had 56px left, which is about six characters before the ellipsis.
function startRename(titleEl, item) {
  // Clicking one row's pencil while another row is being edited blurs that input first, which commits
  // it and rebuilds the list, leaving this handler holding a title span that is no longer on the page.
  // Editing it would strand `renamingId` set and freeze every later redraw.
  if (!titleEl.isConnected) return;
  renamingId = item.id;
  const editor = document.createElement("span");
  editor.className = "conv-edit";
  const input = document.createElement("input");
  input.type = "text";
  input.className = "conv-rename-input";
  input.value = item.title;
  input.setAttribute("aria-label", "Conversation title");
  // The row itself switches conversations on click, and an edit is not a switch.
  editor.addEventListener("click", (e) => e.stopPropagation());
  let settled = false;
  const commit = () => {
    if (settled) return;
    settled = true;
    const title = input.value.trim();
    // An unchanged or emptied title is a cancelled rename: nothing is sent, and the row goes back to
    // what the server last said it was called.
    if (title && title !== item.title && ws.readyState === WebSocket.OPEN) {
      // `replacing` is what this page was showing. The server refuses the write if that is no longer
      // the stored title, so a row a generated title has already moved under does not clobber it.
      ws.send(JSON.stringify({ type: "rename", id: item.id, title, replacing: item.title }));
    }
    endRename(true);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      commit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      settled = true;
      endRename(true);
    }
  });
  input.addEventListener("blur", commit);

  // Asks the model to name the conversation from the whole of what it now holds, which is worth more
  // than the title it got from its opening message alone. It discards whatever is typed, so it closes
  // the editor rather than pretending to still be editing, and the row says "naming" until the answer
  // lands (seconds, on a local model).
  const retitle = document.createElement("button");
  retitle.type = "button";
  retitle.className = "conv-retitle";
  retitle.textContent = "\u2728";
  retitle.title = "Ask the model to name this conversation";
  retitle.setAttribute("aria-label", "Ask the model to name this conversation");
  // Keeps the input from blurring, which would commit the edit this button is replacing.
  retitle.addEventListener("mousedown", (e) => e.preventDefault());
  retitle.addEventListener("click", () => {
    settled = true;
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "retitle", id: item.id }));
      awaitingTitle.add(item.id);
    }
    endRename(true);
  });

  editor.appendChild(input);
  editor.appendChild(retitle);
  titleEl.replaceWith(editor);
  input.focus();
  input.select();
}

function renderConversations() {
  // See `renamingId`: a redraw during an edit would discard the only copy of what the user typed.
  if (renamingId !== null) {
    renderDeferred = true;
    return;
  }
  const nested = nestedConversationIds();
  convList.innerHTML = "";
  let shown = 0;
  for (const item of lastConversations) {
    if (nested.has(item.id)) continue;  // shown under its task instead
    shown += 1;
    const li = document.createElement("li");
    // Title over age in their own column, so the delete button stays at the row's right edge.
    const text = document.createElement("span");
    text.className = "conv-text";
    const title = document.createElement("span");
    title.className = "conv-title";
    if (awaitingTitle.has(item.id)) {
      title.classList.add("naming");
      title.textContent = "naming\u2026";
    } else {
      title.textContent = item.title;
    }
    const age = document.createElement("span");
    age.className = "conv-age";
    age.textContent = relativeTime(item.updated_at);
    text.appendChild(title);
    text.appendChild(age);
    li.appendChild(text);
    if (item.active) {
      li.classList.add("active");
    } else {
      li.addEventListener("click", () => selectConversation(item.id));
    }
    const exportBtn = document.createElement("button");
    exportBtn.type = "button";
    exportBtn.className = "conv-export";
    exportBtn.textContent = "↓";
    exportBtn.title = "Export conversation as Markdown";
    exportBtn.addEventListener("click", (e) => {
      e.stopPropagation();  // don't also trigger switch
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "export", id: item.id }));
    });
    li.appendChild(exportBtn);
    // Copies the whole conversation. Unlike a turn's branch control it does not switch the view, so
    // the label rather than a jump is what tells you it worked; the new row appearing is the rest.
    const duplicate = document.createElement("button");
    duplicate.type = "button";
    duplicate.className = "conv-duplicate";
    duplicate.textContent = "\u29c9";
    duplicate.title = "Duplicate conversation";
    duplicate.setAttribute("aria-label", "Duplicate conversation");  // the glyph is no accessible name
    duplicate.addEventListener("click", (e) => {
      e.stopPropagation();  // don't also trigger switch
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "duplicate", id: item.id }));
    });
    li.appendChild(duplicate);
    const rename = document.createElement("button");
    rename.type = "button";
    rename.className = "conv-rename";
    rename.textContent = "\u270e";
    rename.title = "Rename conversation";
    rename.setAttribute("aria-label", "Rename conversation");  // the glyph is no accessible name
    rename.addEventListener("click", (e) => {
      e.stopPropagation();  // don't also trigger switch
      startRename(title, item);
    });
    li.appendChild(rename);
    const del = document.createElement("button");
    del.type = "button";
    del.className = "conv-delete";
    del.textContent = "×";
    del.title = "Delete conversation";
    del.addEventListener("click", (e) => {
      e.stopPropagation();  // don't also trigger switch
      if (!confirm(`Delete "${item.title}"?`)) return;
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "delete", id: item.id }));
    });
    li.appendChild(del);
    convList.appendChild(li);
  }
  // Blank rather than "0" at zero, as the tasks count is: a section with nothing in it says so by
  // being empty, and a lone zero beside the title reads like a value that failed to load.
  conversationsCount.textContent = shown ? String(shown) : "";
  // The header names what you are looking at, which is the one thing the old dark bar's
  // "Kokua, local, single user" could not tell you. Read from every conversation, not just the
  // unnested ones, so viewing a task's conversation still names it.
  const active = lastConversations.find((item) => item.active);
  convHeading.textContent = active ? active.title : "Kokua";
}

newConvBtn.addEventListener("click", () => {
  if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "new" }));
});

// --- Scheduled tasks section --------------------------------------------------------------------

// Seconds until the next firing, in the sidebar's units.
function countdown(seconds) {
  if (seconds < 60) return "in " + Math.max(1, Math.round(seconds)) + "s";
  if (seconds < 3600) return "in " + Math.round(seconds / 60) + "m";
  if (seconds < 86400) return "in " + Math.round(seconds / 3600) + "h";
  return "in " + Math.round(seconds / 86400) + "d";
}

// A task with nothing to count down to. The server sends the reason as a `status` rather than as
// prose, so the sidebar words it for a person and the agent's tools word it for a model.
const STATUS_TEXT = { disabled: "disabled", past: "overdue", invalid: "bad schedule" };

function scheduleText(schedule) {
  if (!schedule || !schedule.type) return "";
  if (schedule.type === "daily") return "daily " + (schedule.at || "");
  if (schedule.type === "weekly") return (schedule.day || "") + " " + (schedule.at || "");
  if (schedule.type === "once") return "once " + (schedule.at || "");
  if (schedule.type === "interval") {
    const seconds = schedule.seconds || 0;
    if (seconds % 3600 === 0) return "every " + seconds / 3600 + "h";
    if (seconds % 60 === 0) return "every " + seconds / 60 + "m";
    return "every " + seconds + "s";
  }
  return schedule.type;
}

function taskAction(action, name) {
  if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "task", action, name }));
}

function actionButton(label, title, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.title = title;
  button.addEventListener("click", handler);
  return button;
}

function taskRow(task) {
  const running = taskIsRunning(task);
  const li = document.createElement("li");
  li.className = "task-row" + (task.enabled ? " enabled" : "") + (running ? " running" : "");
  li.dataset.taskName = task.name;

  const dot = document.createElement("span");
  dot.className = "task-dot";
  li.appendChild(dot);

  const text = document.createElement("span");
  text.className = "task-text";
  const name = document.createElement("span");
  name.className = "task-name";
  name.textContent = task.name || task.prompt;
  const when = document.createElement("span");
  when.className = "task-when";
  const next = task.next_fire_seconds === null || task.next_fire_seconds === undefined
    ? STATUS_TEXT[task.status] || ""
    : countdown(task.next_fire_seconds);
  when.textContent = [scheduleText(task.schedule), next].filter(Boolean).join(" · ");
  text.appendChild(name);
  text.appendChild(when);
  li.appendChild(text);
  // The full prompt is the one field too long for a row, so it is the row's tooltip.
  li.title = task.prompt;

  const actions = document.createElement("span");
  actions.className = "task-actions";
  actions.appendChild(
    task.enabled
      ? actionButton("⏸", "Disable this task", () => taskAction("disable", task.name))
      : actionButton("▶", "Enable this task", () => taskAction("enable", task.name))
  );
  // Stop takes Run-now's slot for as long as a firing is in flight, the way the composer's Stop takes
  // Send's: running it again while it is already running is not what the button is for at that moment.
  actions.appendChild(
    running
      ? actionButton("■", "Stop the run in progress (the task stays scheduled)", () => taskAction("stop", task.name))
      : actionButton("⟲", "Run this task now", () => taskAction("run", task.name))
  );
  const del = actionButton("×", "Delete this task", () => {
    if (confirm(`Delete the task "${task.name || task.prompt}"?`)) taskAction("delete", task.name);
  });
  del.className = "task-delete";
  actions.appendChild(del);
  li.appendChild(actions);
  return li;
}

function taskConversationRow(conv) {
  const li = document.createElement("li");
  li.className = "task-conv" + (conv.active ? " active" : "") + (conv.running ? " running" : "");
  // When a task runs, every firing's conversation carries the same title (the task's name), so the
  // row shows when it ran instead -- that is the only thing telling two firings apart. The marker
  // keeps a bare date from reading as a stray row rather than a child of the task above.
  const marker = document.createElement("span");
  marker.className = "task-conv-marker";
  marker.textContent = "↳";
  marker.setAttribute("aria-hidden", "true");
  const label = document.createElement("span");
  label.className = "task-conv-label";
  const when = relativeTime(conv.updated_at);
  // A run in flight has nothing to date yet -- its conversation is minted at the start of the firing,
  // so "now" would be the only thing every one of them said. It says what it is doing instead.
  label.textContent = conv.running ? "running…" : (when || conv.title);
  li.appendChild(marker);
  li.appendChild(label);
  li.title = conv.title;
  if (!conv.active) li.addEventListener("click", () => selectConversation(conv.id));
  // A task minting a conversation per firing piles them up here, so each firing needs the delete the
  // chat list has. Appended for the active row too, matching that list: the run you are reading is
  // the one you are most likely to be done with. The prompt names the run rather than the title,
  // which every firing of a task shares and so cannot tell two of them apart.
  const del = document.createElement("button");
  del.type = "button";
  del.className = "task-conv-delete";
  del.textContent = "×";
  del.title = "Delete this run";
  del.addEventListener("click", (e) => {
    e.stopPropagation();  // don't also switch to the conversation being deleted
    if (!confirm(when ? `Delete this run from ${when}?` : `Delete "${conv.title}"?`)) return;
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "delete", id: conv.id }));
  });
  li.appendChild(del);
  return li;
}

function renderTasks() {
  tasksSection.classList.toggle("hidden", lastTasks.length === 0);
  tasksCount.textContent = lastTasks.length ? String(lastTasks.length) : "";
  taskList.innerHTML = "";
  for (const task of lastTasks) {
    taskList.appendChild(taskRow(task));
    for (const conv of conversationsOfTask(task)) taskList.appendChild(taskConversationRow(conv));
  }
}

function renderSidebar() {
  renderConversations();
  renderTasks();
}

// Wire a sidebar section's title row to the list under it. Collapsed state persists like the
// sidebar's own, so a section stays how the user left it, and `aria-expanded` on the toggle is both
// the accessible state and the page's own record of it, which is why nothing else tracks it.
// `storageKey` records *collapsed*, not expanded, so a browser that has never seen the section reads
// null and gets the expanded default.
function wireFoldableSection(toggle, body, storageKey) {
  const setExpanded = (expanded) => {
    toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
    body.hidden = !expanded;
    try {
      localStorage.setItem(storageKey, expanded ? "false" : "true");
    } catch (e) {}
  };
  toggle.addEventListener("click", () => setExpanded(toggle.getAttribute("aria-expanded") !== "true"));
  try {
    if (localStorage.getItem(storageKey) === "true") setExpanded(false);
  } catch (e) {}
}
// A task the model schedules or cancels mid-chat does not push a new task frame, so this is how the
// section catches up without a reload. A firing's new conversation needs no refresh: it arrives on the
// conversations frame and nests client-side.
tasksRefresh.addEventListener("click", () => {
  if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "get_tasks" }));
});

wireFoldableSection(conversationsToggle, convList, "conversations-collapsed");
wireFoldableSection(tasksToggle, taskList, "tasks-collapsed");

// --- Theme -------------------------------------------------------------------------------
// Theme is a per-browser preference (localStorage), applied pre-paint by the head script; the
// header button is the only control. The stored choice is "auto" (follow the OS, the default),
// "light", or "dark" -- three states, so the button cycles rather than toggles, and what it shows
// is the *choice* rather than the resolved theme (under "auto" those differ).
const THEME_CYCLE = ["auto", "light", "dark"];
const THEME_GLYPHS = { auto: "\u25d0", light: "\u2600", dark: "\u263e" };
const THEME_LABELS = { auto: "auto (system)", light: "light", dark: "dark" };
const themeBtn = document.getElementById("theme-btn");

function currentThemeChoice() {
  try {
    const stored = localStorage.getItem("theme");
    return THEME_CYCLE.includes(stored) ? stored : "auto";
  } catch (e) { return "auto"; }
}
function applyThemeChoice(choice) {
  const dark = choice === "auto"
    ? (window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches)
    : choice === "dark";
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  try { localStorage.setItem("theme", choice); } catch (e) {}
  themeBtn.textContent = THEME_GLYPHS[choice];
  const label = "Theme: " + THEME_LABELS[choice];
  themeBtn.title = label;
  themeBtn.setAttribute("aria-label", label);
}
themeBtn.addEventListener("click", () => {
  const next = THEME_CYCLE[(THEME_CYCLE.indexOf(currentThemeChoice()) + 1) % THEME_CYCLE.length];
  applyThemeChoice(next);
});
// The head script already resolved data-theme; this only brings the button in line with it.
applyThemeChoice(currentThemeChoice());

// While "auto" is active, track live OS theme changes.
if (window.matchMedia) {
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (currentThemeChoice() === "auto") applyThemeChoice("auto");
  });
}

// Left panel: collapse/expand + drag-resize. Width and collapsed state are per-browser
// preferences (localStorage), applied pre-paint by the head script. Dragging narrower than
// SNAP_WIDTH collapses to the icon rail; dragging back out re-expands.
const sidebar = document.getElementById("sidebar");
const sidebarResize = document.getElementById("sidebar-resize");
const sidebarToggle = document.getElementById("sidebar-toggle");
const SIDEBAR_MIN = 180, SIDEBAR_MAX = 480, SIDEBAR_SNAP = 120;
function currentSidebarWidth() {
  const w = parseInt(localStorage.getItem("sidebar-width"), 10);
  return (w >= SIDEBAR_MIN && w <= SIDEBAR_MAX) ? w : 240;
}
function applySidebarWidth(px) {
  const w = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, Math.round(px)));
  document.documentElement.style.setProperty("--sidebar-width", w + "px");
  try { localStorage.setItem("sidebar-width", String(w)); } catch (e) {}
}
function setSidebarCollapsed(collapsed) {
  const root = document.documentElement;
  if (collapsed) root.setAttribute("data-sidebar-collapsed", "true");
  else root.removeAttribute("data-sidebar-collapsed");
  sidebarToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  sidebarToggle.textContent = collapsed ? "»" : "«";
  sidebarToggle.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
  try { localStorage.setItem("sidebar-collapsed", collapsed ? "true" : "false"); } catch (e) {}
}
// Reflect the pre-paint state in the toggle control at load.
setSidebarCollapsed(document.documentElement.getAttribute("data-sidebar-collapsed") === "true");
sidebarToggle.addEventListener("click", () => {
  setSidebarCollapsed(document.documentElement.getAttribute("data-sidebar-collapsed") !== "true");
});
// Resize width from the pointer position relative to the sidebar's left edge; snap to the rail below
// the threshold. Pointer capture keeps tracking even if the cursor leaves the thin handle.
function resizeFromClientX(clientX) {
  const width = clientX - sidebar.getBoundingClientRect().left;
  if (width < SIDEBAR_SNAP) { setSidebarCollapsed(true); return; }
  setSidebarCollapsed(false);
  applySidebarWidth(width);
}
sidebarResize.addEventListener("pointerdown", (e) => {
  e.preventDefault();
  sidebarResize.setPointerCapture(e.pointerId);
  document.body.classList.add("resizing");
});
sidebarResize.addEventListener("pointermove", (e) => {
  if (!sidebarResize.hasPointerCapture(e.pointerId)) return;
  resizeFromClientX(e.clientX);
});
sidebarResize.addEventListener("pointerup", (e) => {
  sidebarResize.releasePointerCapture(e.pointerId);
  document.body.classList.remove("resizing");
});
sidebarResize.addEventListener("keydown", (e) => {
  const collapsed = document.documentElement.getAttribute("data-sidebar-collapsed") === "true";
  if (e.key === "Enter") { setSidebarCollapsed(!collapsed); e.preventDefault(); }
  else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
    const delta = e.key === "ArrowRight" ? 16 : -16;
    const base = collapsed ? SIDEBAR_MIN : currentSidebarWidth();
    if (collapsed && delta > 0) setSidebarCollapsed(false);
    applySidebarWidth(base + delta);
    e.preventDefault();
  }
});

let streamingBubble = null;  // the assistant answer bubble accumulating tokens
let streamingText = "";      // raw answer text, the source of truth every live render reparses
let thinkingBlock = null;    // the reasoning block accumulating THINKING tokens
let subagentCards = {};      // sub-agent card id -> element, so a "running" card updates on its verdict
// The bubbles that opened live turns whose `turn_saved` has not arrived yet, oldest first, each tagged
// with the conversation it was drawn for. A queue rather than one reference because sending a follow-up
// while a reply is still streaming is ordinary: the server queues that turn behind the first (turns on
// one conversation serialize in submission order, so their saves land in that order too), and a single
// reference would by then point at the newer bubble and stamp it with the older turn's index. That is
// worse than losing the control, because the index is a valid turn boundary the server will honour, so
// the user would click delete on one message and lose that one and the message before it.
//
// Two residual cases, in opposite directions. A proactive run in the conversation being viewed, landing
// its own `turn_saved` while the user has a bubble waiting here, consumes that entry and stamps it with
// the proactive turn's index instead; it needs a proactive turn in the very conversation the user just
// sent to. The other is worse and is why nothing is enqueued for text the server answers as a command:
// an entry that is never consumed (a turn that never reaches its save) sits at the head forever, so the
// *next* turn's `turn_saved` takes the stale entry and stamps the older bubble with the newer turn's
// index, leaving every control on the page shifted by one turn from then on. Both are repaired by any
// repaint, since the replay path derives indices from the server rather than from this queue.
let pendingTurnBubbles = [];

// Render/update a sub-agent card as a foldable block. Two producers share this frame type: a
// planning reviewer (role + status + issues) and a spawned sub-agent (role + task + status, its
// body built up from `append` frames). A spawn card is the one carrying `task`.
function renderSubagent(frame, ts) {
  let card = frame.id ? subagentCards[frame.id] : null;
  if (!card) {
    const spawn = !!frame.task;
    card = addFoldable("subagent" + (spawn ? " spawn" : ""), {}, { md: true }, ts);  // stamp once, when first created
    if (frame.id) subagentCards[frame.id] = card;
    card.spawn = spawn;
    card.answers = [];  // every generated-text block, re-rendered as markdown when the spawn ends
    if (spawn) card.body.appendChild(spawnArgsLine(frame));
    // An `append` frame carries no role or status, so a card first created by one has no identity to
    // put on its header. Label it anyway: an empty header reads as a broken block rather than as the
    // sub-agent output it holds. Overwritten below by the first frame that does carry identity.
    setFoldLabel(card.label, "subagent", "Sub-agent", "working…");
  }
  if (frame.append) appendSubagentEntry(card, frame.append);
  // A spawn's finish frame carries both `status` and `append` together, and no `role` of its own
  // (see the frame shapes above) -- remember the role on the card so a status-only update still
  // has the identity to re-render, rather than either skipping the label update (frame.append is
  // truthy) or falling back to "Sub-agent" (frame.role is absent).
  if (frame.role || frame.status) {
    if (frame.role) card.role = frame.role;
    const role = card.role || "Sub-agent";
    const status = subagentStatusLabel(frame.status, card.spawn);
    // A spawn reads as the call it replaces; the role sits in the header because it is what
    // tells several concurrent spawns apart while they are all collapsed. Its full arguments,
    // task included, are on the card's own argument line. The kind word is what tells a reviewer's
    // card from a spawn's at a glance, which is the job the 🔎/🔧 icons used to do.
    if (card.spawn) setFoldLabel(card.label, "subagent", SPAWN_TOOL_NAME + "(" + role + ")", status);
    else setFoldLabel(card.label, "review", role, status);
    if (frame.issues && frame.issues.length && frame.status !== "running") {
      const issuesMd = frame.issues.map((i) => "- " + i).join("\n");
      const html = renderMarkdown(issuesMd);
      if (html === null) card.body.textContent = issuesMd;
      else {
        card.body.innerHTML = html;
        typesetMath(card.body);
      }
    }
  }
  if (frame.status) card.outer.dataset.status = frame.status;
  if (frame.status && frame.status !== "running") finalizeSubagentCard(card);
  autoscroll();
}

function subagentStatusLabel(status, isSpawn) {
  if (status === "running") return isSpawn ? "working…" : "reviewing…";
  return status || "done";
}

// The tool whose block the server suppresses in favour of the card (see SPAWN_SUBAGENT_TOOL_NAME
// in channels/web.py); the card names it so the transcript still says which call was made.
const SPAWN_TOOL_NAME = "spawn_subagent";

// A spawn card's first body line: the call's arguments, rendered exactly as a tool block's body
// is. `agent_type` is reconstructed from the frame's role because kokua always builds AIMU's
// typed spawn tool, whose parameters are agent_type and task (see toolsets/agents.py's build_agent_specs);
// AIMU's untyped mode, which kokua never builds, would take task alone.
function spawnArgsLine(frame) {
  const el = document.createElement("div");
  el.className = "sa-args";
  el.textContent = toolArgs({ agent_type: frame.role || "subagent", task: frame.task });
  return el;
}

// One entry inside a card's body, built from the very same components the top level uses for the
// same thing: a 💭 thinking foldable, a 🔧 <name> tool foldable, and the generated text as its own
// foldable rendered as markdown. Consecutive entries of one kind extend the open block, so
// streamed text reads as prose rather than as one block per token, and an entry of another kind
// closes it: that is what gives a multi-round sub-agent one thinking and one answer block per tool
// round, the way a tool block separates the parent's own rounds. An injected round (a continuation
// nudge, or the forced wrap-up at the cap) has no tool call to separate it, so it carries its own row
// here instead.
function appendSubagentEntry(card, entry) {
  if (entry.kind === "reasoning") {
    card.answer = null;
    if (!card.reasoning) card.reasoning = addFoldable("thinking", { kind: "thinking" }, { parent: card.body });
    card.reasoning.body.appendChild(document.createTextNode(entry.text || ""));
    setFoldLabel(card.reasoning.label, "thinking", "", lineMetric(card.reasoning.body.textContent));
    return;
  }
  card.reasoning = null;
  if (entry.kind === "loop") {
    card.answer = null;
    renderLoop(entry.text, undefined, { parent: card.body, reason: entry.reason });
    return;
  }
  if (entry.kind === "tool") {
    card.answer = null;
    renderTool(entry.name, entry.arguments, undefined, { parent: card.body, response: entry.response });
    return;
  }
  if (entry.kind === "error") {
    card.answer = null;
    const el = document.createElement("div");
    el.className = "sa-error";
    // Carries the same kind word every other block does, so the transcript reads one way throughout.
    // Not a foldable: a failure is one short line and the reason the reader is looking.
    const kind = document.createElement("span");
    kind.className = "fold-kind";
    kind.textContent = "error";
    el.appendChild(kind);
    el.appendChild(document.createTextNode(entry.text || ""));
    card.body.appendChild(el);
    return;
  }
  // Generated text: appended as plain text while it streams and re-rendered as markdown at the
  // terminal status, the same two steps the assistant's own reply takes (see finalizeStreaming).
  // Expanded from the start, since it is the result a reader opens the card for.
  if (!card.answer) {
    const block = addFoldable("assistant", { kind: "answer" }, { parent: card.body, expanded: true });
    card.answer = { body: block.body, text: "" };
    card.answers.push(card.answer);
  }
  card.answer.text += entry.text || "";
  card.answer.body.textContent = card.answer.text;
}

// Close a card's open blocks and render each of its answer blocks as markdown. Runs on the
// spawn's terminal status, which a replayed card also ends with, so live and replayed cards end
// up with the same DOM.
function finalizeSubagentCard(card) {
  card.reasoning = null;
  card.answer = null;
  for (const answer of card.answers) {
    const html = renderMarkdown(answer.text);
    if (html === null) continue;  // markdown libs absent: the streamed plain text stands
    answer.body.classList.add("md");
    answer.body.innerHTML = html;
    typesetMath(answer.body);
  }
}

// A turn is being processed: Stop takes the primary slot for its duration (a `done` frame, or a
// non-proactive `message` for "(stopped)"/errors, hands it back). Also called with false on a
// `history` frame (a view change) and with the `working` frame's state, so the composer always
// describes the conversation being viewed rather than the last one to start a turn. Without that
// reset, switching away from a running turn would leave the new conversation showing Stop and no way
// to send at all.
function setProcessing(active) {
  sendBtn.classList.toggle("hidden", active);
  stopBtn.classList.toggle("hidden", !active);
}

// Follow streamed output only while the user is already at the bottom. Once they scroll up we
// stop yanking the view down, so they can read earlier text mid-generation; scrolling back to
// the bottom re-enables following. Updated from the user's own scrolls (programmatic scrolls to
// the bottom just keep it true).
let stickToBottom = true;
function atBottom() {
  return log.scrollHeight - log.scrollTop - log.clientHeight < 40;  // small slack for rounding
}
function autoscroll() { if (stickToBottom) log.scrollTop = log.scrollHeight; }
log.addEventListener("scroll", () => { stickToBottom = atBottom(); });

// A datetime value (an ISO string from history, or a Date for a live bubble) split into a short
// label and a full-precision tooltip, or null when unparseable/absent so callers render no caption.
function tsParts(value) {
  if (value == null) return null;
  const d = value instanceof Date ? value : new Date(value);
  if (isNaN(d.getTime())) return null;
  return {
    label: d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }),
    full: d.toLocaleString(),
  };
}
// The cluster a finished bubble grows: its datetime caption and, once the turn it ends is known, its
// branch and delete controls. It is the bubble's last flex track, so it rides the block's first line
// at the right edge, in the column a foldable's `.fold-ts` occupies; because a flex track is laid out
// beside the content rather than over it, however long the answer runs it wraps before the caption
// instead of underneath it. Returns the existing row on a second call, so the caption and the controls
// (added at different times: the caption when the bubble is stamped, the controls once the turn ends)
// land in the same row rather than each growing their own.
//
// Tracked in a WeakMap keyed by the bubble element, NOT looked up by `el.querySelector(".bubble-meta")`:
// do not "simplify" this back to one. An assistant bubble's own content is untrusted (model or tool
// output; see the note above renderMarkdown), and DOMPurify sanitizes it without restricting
// attributes, so a `class` survives: a reply containing literal `<div class="bubble-meta">...</div>`
// would be a perfectly valid match for that selector, and a class-based lookup can't tell it apart
// from the row this function creates. Landing the caption or the branch control inside
// attacker-controlled markup that way silently reintroduces the misplacement (or the invisibility)
// this row exists to prevent. A WeakMap entry has no representation in HTML at all, so nothing a
// model or a tool result writes can forge a hit in it: the row this function returns is always the
// one it created itself.
const bubbleMetaRows = new WeakMap();
function bubbleMetaRow(el) {
  let row = bubbleMetaRows.get(el);
  if (!row) {
    row = document.createElement("div");
    row.className = "bubble-meta";
    el.appendChild(row);
    bubbleMetaRows.set(el, row);
  }
  return row;
}
// A bubble is a flex row of the same parts a foldable header has, and this is the middle one: the
// wrapper holding everything the block actually says. Content goes here rather than straight into the
// bubble so the datetime caption and the turn's controls can be a flex track beside it (see
// `bubbleMetaRow`), which is what puts them on the block's first line at its right edge without ever
// landing over wrapped text.
//
// This is also what makes a content write safe. renderStreaming and finalizeStreaming replace the
// whole of what they render into, and what they render into is now this wrapper: the caption is a
// sibling, so a late render tick can no longer detach it.
//
// WeakMap-tracked, for the same reason and with the same warning as the meta row above: do not
// "simplify" this into `el.querySelector(".bubble-body")`.
const bubbleBodies = new WeakMap();
function bubbleBody(el) {
  let body = bubbleBodies.get(el);
  if (!body) {
    body = document.createElement("div");
    body.className = "bubble-body";
    // Before the meta row, when there is one, so the caption stays the row's last track. Only
    // reachable if a block is stamped before it has content, which nothing does today.
    const meta = bubbleMetaRows.get(el);
    if (meta && meta.parentNode === el) el.insertBefore(body, meta);
    else el.appendChild(body);
    bubbleBodies.set(el, body);
  }
  return body;
}
// Caption a regular bubble with its datetime, in the meta cluster on the block's first line (below
// the content on a notice, which is not a flex row). No-op when value is absent.
function stampBubble(el, value) {
  const t = tsParts(value);
  if (!t) return;
  const span = document.createElement("span");
  span.className = "bubble-ts";
  span.textContent = t.label;
  span.title = t.full;
  bubbleMetaRow(el).appendChild(span);
}
// A turn's branch control, appended to the bubble that opens the turn, beside the delete-from-here
// control: both act on a whole turn, so both belong on the one bubble that names it rather than
// scattered across the turn's output. `messageIndex` is the position of the turn's user message in
// the stored transcript, which is how the server names a turn; the page never computes it, because a
// count of rendered bubbles is not the same number.
// `conversationId` is captured at the time the bubble is stamped (the live conversation for a fresh
// turn, the conversation being replayed for one loaded from history) rather than read from
// `lastConversations` at click time: the active conversation can change between the stamp and the
// click, and a click must fork the conversation the bubble was actually drawn for.
function addBranchControl(el, messageIndex, conversationId) {
  if (!el || el.dataset.branchIndex !== undefined || !Number.isInteger(messageIndex) || messageIndex < 0) return;
  if (!conversationId) return;
  el.dataset.branchIndex = String(messageIndex);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "bubble-branch";
  // U+2387, a line splitting away from the line it was on, which is what a branch does to a
  // transcript. Escaped rather than pasted because the shape it draws is font-dependent, so the
  // codepoint is the durable statement of which glyph was asked for; the title and aria-label below
  // carry the meaning either way, for a browser whose fonts do not cover it as much as for a reader.
  btn.textContent = "\u2387";
  btn.title = "Branch a new conversation including this exchange";
  btn.setAttribute("aria-label", "Branch a new conversation including this exchange");
  btn.addEventListener("click", () => {
    if (ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "branch", id: conversationId, message_index: messageIndex }));
  });
  bubbleMetaRow(el).appendChild(btn);
}
// A turn's delete-from-here control, appended to the bubble that opens the turn. `messageIndex` and
// `conversationId` are read exactly as `addBranchControl` reads them, and for the same reasons: the
// page never computes an index (a count of rendered bubbles is not the same number), and the
// conversation is captured at stamp time because the active one can change before the click.
//
// The glyph is the sidebar's delete "×" rather than something bespoke, so the destructive affordance
// looks the same wherever it appears; what it deletes is in the label and in the confirmation, since
// a glyph cannot say "and everything after this".
function addTruncateControl(el, messageIndex, conversationId) {
  if (!el || el.dataset.truncateIndex !== undefined || !Number.isInteger(messageIndex) || messageIndex < 0) return;
  if (!conversationId) return;
  el.dataset.truncateIndex = String(messageIndex);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "bubble-truncate";
  btn.textContent = "×";
  btn.title = "Delete this turn and everything after it";
  btn.setAttribute("aria-label", "Delete this turn and everything after it");
  btn.addEventListener("click", () => {
    if (ws.readyState !== WebSocket.OPEN) return;
    if (!confirm("Delete this turn and everything after it? This cannot be undone.")) return;
    ws.send(JSON.stringify({ type: "truncate", id: conversationId, message_index: messageIndex }));
  });
  bubbleMetaRow(el).appendChild(btn);
}
// Both of a turn's controls, on the bubble that opens it. One helper rather than two calls at each
// site so the pair cannot drift apart: they act on the same turn and read the same index, and the
// order they are appended in is the order they appear, constructive before destructive.
function stampTurnControls(bubble, messageIndex, conversationId) {
  addBranchControl(bubble, messageIndex, conversationId);
  addTruncateControl(bubble, messageIndex, conversationId);
}
// Caption a foldable block on its header line, so the time shows whether collapsed or expanded.
function stampHeader(header, value) {
  const t = tsParts(value);
  if (!t) return;
  const span = document.createElement("span");
  span.className = "fold-ts";
  span.textContent = t.label;
  span.title = t.full;
  header.appendChild(span);
}

function addBubble(cls, text, ts) {
  const el = document.createElement("div");
  el.className = "bubble " + cls;
  // The user's turn is marked, not filled: a flat transcript has no sides to alternate between, so
  // the glyph is what says who is speaking. A sibling of the body rather than part of it, so it is
  // the row's first flex track and the text wraps to its right instead of under it.
  if (cls === "user") {
    const marker = document.createElement("span");
    marker.className = "row-marker";
    marker.textContent = ">";
    el.appendChild(marker);
  }
  bubbleBody(el).textContent = text || "";
  stampBubble(el, ts);
  appendToLog(el);
  autoscroll();
  return el;
}

function notice(text) { return addBubble("notice", text); }

// A collapsed row's label, in three parts: the kind word, the payload (the call with its condensed
// arguments), and an optional metric. They are separate spans so the payload is the only part that
// ellipsizes, which is what keeps a row exactly one line tall however long the arguments are. All
// three live inside .fold-label, so the label's text is still the whole line.
//
// A row with no payload (thinking, continuation, plan) is labelled `no-payload`, because there the
// kind word is the entire visible line and has to be the row's primary content. Wearing the dimmer
// secondary styling it takes beside a tool call's arguments, such a row reads as blank space.
function setFoldLabel(label, kind, payload, metric) {
  label.replaceChildren();
  label.classList.toggle("no-payload", !payload);
  for (const [cls, text] of [["fold-kind", kind], ["fold-payload", payload], ["fold-metric", metric]]) {
    if (!text) continue;
    const span = document.createElement("span");
    span.className = cls;
    span.textContent = text;
    label.appendChild(span);
  }
}

// "N lines" for a folded text block. With the body hidden by default this count is the only signal
// that reasoning happened at all, and roughly how much of it there was.
function lineMetric(text) {
  const lines = (text || "").split("\n").length;
  return lines + (lines === 1 ? " line" : " lines");
}

// Text that would render as an empty bubble. A server that separates reasoning from the answer
// itself sends the newlines that followed the reasoning as the answer segment's first tokens, so a
// segment can be whitespace and nothing else; the bubble it would open carries only its timestamp.
function isBlank(text) {
  return !text || !text.trim();
}

// A foldable auxiliary block: a header (identifying label, always visible) over a body (verbose
// detail, hidden until expanded). Starts collapsed. Returns handles so callers can populate/stream
// into `body` and update `label` (e.g. a sub-agent card whose status changes). `labelParts` is
// `{kind, payload, metric}`; only the payload ellipsizes, so the arguments belong there.
// `opts.md` marks the body as markdown-rendered content (drops plain-text pre-wrap; see the .md
// rules). `opts.parent` places the block inside another element instead of the transcript, which is
// how a sub-agent card's nested blocks are the very same components as their top-level counterparts;
// `opts.expanded` starts it open, for a block whose content is the point rather than the detail.
function addFoldable(cls, labelParts, opts, ts) {
  const expanded = !!(opts && opts.expanded);
  const outer = document.createElement("div");
  outer.className = "bubble foldable " + (expanded ? "" : "collapsed ") + cls;
  const header = document.createElement("button");
  header.type = "button";
  header.className = "fold-header";
  header.setAttribute("aria-expanded", expanded ? "true" : "false");
  const tri = document.createElement("span");
  tri.className = "fold-tri";
  tri.textContent = expanded ? "▾" : "▸";
  const label = document.createElement("span");
  label.className = "fold-label";
  const parts = labelParts || {};
  setFoldLabel(label, parts.kind, parts.payload, parts.metric);
  header.appendChild(tri);
  header.appendChild(label);
  stampHeader(header, ts);  // rides the header so it stays visible while collapsed
  const body = document.createElement("div");
  body.className = "fold-body" + (opts && opts.md ? " md" : "");
  // `opts.onFirstExpand` fills the body the first time it is opened and never again, so a block
  // holding something expensive (a large tool result) costs nothing until a reader asks for it.
  let fill = (opts && opts.onFirstExpand) || null;
  header.addEventListener("click", () => {
    const collapsed = outer.classList.toggle("collapsed");
    header.setAttribute("aria-expanded", collapsed ? "false" : "true");
    tri.textContent = collapsed ? "▸" : "▾";
    if (!collapsed && fill) {
      const run = fill;
      fill = null;
      run(body);
    }
  });
  outer.appendChild(header);
  outer.appendChild(body);
  appendToLog(outer, opts && opts.parent);
  autoscroll();
  return { outer, header, body, label };
}

// An image in the chat: an uploaded image echoed under the user's turn, or one the assistant
// produced/surfaced. `src` is a /images/<name> reference (server route) or a data: URL (local echo).
function addImageBubble(src, cls, ts) {
  const el = document.createElement("div");
  el.className = "bubble image " + (cls || "assistant");
  const img = document.createElement("img");
  img.src = src;
  img.alt = "image";
  bubbleBody(el).appendChild(img);
  stampBubble(el, ts);
  appendToLog(el);
  autoscroll();
  return el;
}

// --- Markdown rendering (vendored marked + DOMPurify) -------------------------------------
// marked parses full GFM (incl. tables); DOMPurify sanitizes its HTML, since the assistant and
// tool output are untrusted. Force links to open safely. If the libs failed to load, fall back
// to plain text so a bad render never injects markup or crashes the page.
const markdownReady = typeof marked !== "undefined" && typeof DOMPurify !== "undefined";
if (markdownReady) {
  marked.setOptions({ gfm: true, breaks: false });
  DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    if (node.tagName === "A" && node.getAttribute("href")) {
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer");
    }
  });
}

function renderMarkdown(src) {
  if (!markdownReady) return null;  // signal: caller should fall back to plain text
  return DOMPurify.sanitize(marked.parse(src || ""));
}

// Typeset LaTeX math ($...$, $$...$$, \(...\), \[...\]) in an element that already holds sanitized
// HTML. KaTeX runs AFTER DOMPurify and builds its own DOM from each delimiter's TeX; trust:false
// blocks \href/\includegraphics and maxExpand bounds macro expansion, so untrusted model/tool
// output can neither inject markup nor hang the renderer. throwOnError:false leaves a bad
// expression as red source text rather than breaking the bubble. KaTeX's default ignoredTags keep
// any $...$ inside <code>/<pre> literal. No-op if the KaTeX libs failed to load.
const mathReady = typeof renderMathInElement !== "undefined";
function typesetMath(el) {
  if (!mathReady || !el) return;
  try {
    renderMathInElement(el, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "\\(", right: "\\)", display: false },
        { left: "$", right: "$", display: false },
      ],
      throwOnError: false,
      trust: false,
      maxExpand: 1000,
    });
  } catch (e) {
    /* never let a math error break rendering */
  }
}

// --- Live render of a partly-arrived answer ------------------------------------------------
// The whole accumulated buffer is reparsed on a timer rather than committing finished blocks as they
// land, because a newline is not a block boundary in CommonMark: with `breaks:false` a lone newline
// continues the paragraph, and a newline inside a fence, list, or table sits *inside* an open block.
// A committed prefix would therefore be wrong in a way no later token could repair, where reparsing
// converges on the finalized render by construction. The timer matters because a reparse per token is
// quadratic over a long answer. KaTeX is deliberately left to finalizeStreaming: it is the expensive
// half of the render, and half-typed TeX is noise.
const STREAM_RENDER_MS = 50;
let streamRenderTimer = null;

function renderStreaming() {
  streamRenderTimer = null;
  if (!streamingBubble) return;
  const body = bubbleBody(streamingBubble);
  const html = renderMarkdown(streamingText);
  if (html === null) body.textContent = streamingText;
  else {
    body.classList.add("md");
    body.innerHTML = html;
  }
  autoscroll();
}

function scheduleStreamRender() {
  if (streamRenderTimer === null) streamRenderTimer = setTimeout(renderStreaming, STREAM_RENDER_MS);
}

function cancelStreamRender() {
  if (streamRenderTimer !== null) clearTimeout(streamRenderTimer);
  streamRenderTimer = null;
}

function addMarkdownBubble(cls, text, ts) {
  const el = document.createElement("div");
  el.className = "bubble " + cls;
  const body = bubbleBody(el);
  const html = renderMarkdown(text);
  if (html === null) body.textContent = text || "";
  else {
    body.classList.add("md");
    body.innerHTML = html;
    typesetMath(body);
  }
  stampBubble(el, ts);
  appendToLog(el);
  autoscroll();
  return el;
}

// Finalize the open streaming answer bubble (render its accumulated markdown) and reset streaming
// state. Shared by the `done` terminator and the `phase` divider (verbose trace), where each phase
// closes the previous phase's bubble so the next call's output starts a fresh one.
function finalizeStreaming() {
  // Cancel first: a tick landing after this returns would reparse the buffer into a body that has
  // already been typeset, undoing typesetMath and leaving the answer's math as source text. The
  // caption is safe either way, being a sibling of the body rather than inside it.
  cancelStreamRender();
  if (streamingBubble) {
    const body = bubbleBody(streamingBubble);
    const html = renderMarkdown(streamingText);
    if (html !== null) {
      body.classList.add("md");
      body.innerHTML = html;
      typesetMath(body);
    }
    // A live answer has no server timestamp; stamp it with the completion time.
    stampBubble(streamingBubble, new Date());
  }
  streamingBubble = null;
  thinkingBlock = null;
}

const proto = location.protocol === "https:" ? "wss" : "ws";
// Reassigned by connect() on every reconnect, so the `ws.readyState` guards and `ws.send` calls
// throughout this file reach the current socket rather than a dead one. Opened at the bottom of the
// file, once the handlers connect() installs are defined.
let ws;

// The argument portion of a tool call, as a single line. Used as a foldable tool block's body and
// (wrapped by toolLine) in the approval prompt.
function toolArgs(args) {
  if (args && typeof args === "object") {
    return Object.entries(args).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ");
  } else if (args != null) {
    return String(args);
  }
  return "";
}

function toolLine(name, args) {
  return `${name || "tool"}(${toolArgs(args)})`;
}

// How much of a tool result is rendered before a reader has to ask for the rest. Output travels
// whole (the frame carries it all), so this bounds only the DOM: a multi-megabyte result must not
// become a multi-megabyte text node just because someone opened its card.
const OUTPUT_CLAMP = 4000;

// What a tool call returned, as its own nested foldable below the arguments, so the arguments stay
// scannable when the card is opened. Populated on first expand, and clamped to OUTPUT_CLAMP with a
// button for the rest. Plain text only, never markdown: a tool result is untrusted input.
function renderToolOutput(parent, response) {
  const size = response.length.toLocaleString();
  // Not also `.tool`: that would make `.bubble.tool` match a card and its own output. The monospace
  // type and colour are inherited properties, so they arrive from the enclosing card regardless.
  addFoldable("tool-output", { kind: "output", metric: `${size} chars` }, {
    parent,
    onFirstExpand: (body) => {
      const text = document.createElement("span");
      text.className = "output-text";
      text.textContent = response.slice(0, OUTPUT_CLAMP);
      body.appendChild(text);
      if (response.length <= OUTPUT_CLAMP) return;
      const more = document.createElement("button");
      more.type = "button";
      more.className = "output-more";
      more.textContent = `show all (${size} chars)`;
      more.addEventListener("click", () => {
        text.textContent = response;
        more.remove();
      });
      body.appendChild(more);
    },
  });
}

// Foldable tool / continuation blocks. The header carries the call and its condensed arguments, so a
// folded row says what was called without being opened; the body holds the full untruncated arguments
// and, for a tool call, the result it returned. Shared by live frames, history replay, and (via
// `opts.parent`) a sub-agent card's nested tool calls.
function renderTool(name, args, ts, opts) {
  // An absent response means no result was recorded (a transcript stored before results were
  // replayed, a turn cut short mid-dispatch); an empty one means the tool returned nothing. Neither
  // has anything to show, and an "output (0 chars)" row on every such card would be noise.
  const response = opts && opts.response;
  const returned = typeof response === "string" && response;
  const metric = returned ? `${response.length.toLocaleString()} chars` : "";
  const parts = { kind: "tool", payload: toolLine(name, args), metric };
  const f = addFoldable("tool", parts, { parent: opts && opts.parent }, ts);
  f.body.appendChild(document.createTextNode(toolArgs(args)));
  if (returned) renderToolOutput(f.body, response);
  return f;
}
// The kind word each injection wears. AIMU raises the loop's counter for a tool round too, and this
// marker is deliberately not drawn there (the tool block is that boundary), so both words below
// describe a round AIMU drove itself: a nudge after a turn that came back empty, or the forced
// tools-disabled wrap-up at the round cap.
//
// Two fallbacks, and they answer differently on purpose. An absent reason means a producer that did
// not send the key, and reads as the commoner of the two. An *unrecognized* reason renders as itself,
// because a marker asserting the wrong injection is the bug this marker exists to remove: if AIMU
// adds a third kind, "continuation" would be a claim that is wrong, where the raw word is merely odd.
const LOOP_KINDS = { continuation: "continuation", final_answer: "tool-use limit" };

function renderLoop(text, ts, opts) {
  const reason = (opts && opts.reason) || "continuation";
  // `hasOwnProperty`, not a truthiness test on the lookup: a reason of "toString" would otherwise
  // find Object.prototype's method and label the row with a function.
  const kind = Object.prototype.hasOwnProperty.call(LOOP_KINDS, reason) ? LOOP_KINDS[reason] : reason;
  const f = addFoldable("loop", { kind }, { parent: opts && opts.parent }, ts);
  f.body.textContent = text || "";
  return f;
}

// A deep-planning plan as its own foldable, markdown-rendered. Shared by the live `plan` frame and
// the catch-up replay of a planned turn still in flight.
function renderPlan(text) {
  const f = addFoldable("plan", { kind: "plan" }, { md: true });
  const html = renderMarkdown(text);
  if (html === null) f.body.textContent = text || "";
  else {
    f.body.innerHTML = html;
    typesetMath(f.body);
  }
  return f;
}

// Verbose-trace phase header: a labeled divider (e.g. "Planner · drafting a plan"). Shared by the
// live `phase` frame and history replay so both render the raw trace identically.
function renderPhase(label, detail, ts) {
  // A phase is a labelled divider rather than an event row, so its label is the payload alone.
  const f = addFoldable("phase", { payload: label || "" }, undefined, ts);
  if (detail) f.body.textContent = "· " + detail;
  return f.outer;
}

function handleFrame(event) {
  frameReceived = true;
  const frame = JSON.parse(event.data);
  // Blocks are appended in arrival order (thinking -> tool -> answer, possibly several
  // rounds per turn). A tool call or answer token closes any open thinking block.
  if (frame.type === "thinking") {
    // thinkingBlock holds the whole foldable, not just its body: tokens accumulate in the body
    // (collapsed by default; the user expands to watch it), and with the body hidden the line count
    // on the header is the only thing that shows reasoning happened, so it grows as tokens land.
    if (!thinkingBlock) thinkingBlock = addFoldable("thinking", { kind: "thinking" }, undefined, new Date());
    thinkingBlock.body.appendChild(document.createTextNode(frame.text));
    setFoldLabel(thinkingBlock.label, "thinking", "", lineMetric(thinkingBlock.body.textContent));
    autoscroll();
  } else if (frame.type === "tool") {
    thinkingBlock = null;
    renderTool(frame.name, frame.arguments, new Date(), { response: frame.response });
  } else if (frame.type === "loop") {
    // Agent-loop continuation boundary. Like a tool block, it ends the answer segment above it: the
    // next tokens open their own bubble below this marker.
    thinkingBlock = null;
    renderLoop(frame.text, new Date(), { reason: frame.reason });
  } else if (frame.type === "token") {
    thinkingBlock = null;
    // A block that landed since the last token (a tool call, a loop marker, an image) ended the answer
    // segment it interrupted, so close that bubble and start a new one below. Moving the old bubble
    // down instead would drag prose the user already watched arrive underneath the cards it preceded.
    if (streamingBubble && lastContentElement() !== streamingBubble) finalizeStreaming();
    // Only the *opening* is guarded: inside a bubble the same whitespace is real spacing between
    // words the reader can see, while ahead of one it is a section with nothing in it.
    if (!streamingBubble && isBlank(frame.text)) return;
    if (!streamingBubble) {
      streamingBubble = addBubble("assistant", "");
      streamingText = "";
    }
    streamingText += frame.text;
    scheduleStreamRender();
  } else if (frame.type === "done") {
    finalizeStreaming();
    autoscroll();
    setProcessing(false);
    setWorking(null);
  } else if (frame.type === "phase") {
    // Verbose trace: close the previous phase's streamed bubble and start a labeled section.
    finalizeStreaming();
    renderPhase(frame.label, frame.detail, new Date());
  } else if (frame.type === "image") {
    // A generated image finished (or the assistant surfaced one). It lands below the answer so far, and
    // closes it: whatever the assistant writes about the image belongs under the image.
    thinkingBlock = null;
    addImageBubble(frame.url, "assistant", new Date());
  } else if (frame.type === "message") {
    addMarkdownBubble(frame.proactive ? "proactive" : "assistant", frame.text, new Date());
    // A reactive message ends the turn (e.g. "(stopped)" / an error) and takes the indicator with it.
    // A proactive one interrupts whatever is going on without ending it, so it leaves both alone: the
    // indicator belongs to the turn now, not to "the last thing that arrived".
    if (!frame.proactive) {
      setProcessing(false);
      setWorking(null);
    }
  } else if (frame.type === "notification") {
    // A background turn on some OTHER conversation finished (never the one being viewed -- the
    // server only sends this when you've switched away); show it without touching this view or its
    // own working indicator.
    showNotification(frame.text);
  } else if (frame.type === "working") {
    setProcessing(!!frame.active);
    // `elapsed` is how long the turn has been running server-side, so switching into one that started
    // minutes ago counts on from there rather than restarting at zero.
    setWorking(frame.active ? (frame.elapsed || 0) * 1000 : null);
  } else if (frame.type === "approval") {
    renderApproval(frame.name, frame.arguments);
  } else if (frame.type === "plan") {
    renderPlan(frame.text);
  } else if (frame.type === "subagent") {
    renderSubagent(frame, new Date());
  } else if (frame.type === "plan_review") {
    renderPlanReview(frame.plan, frame.critique);
  } else if (frame.type === "conversations") {
    lastConversations = frame.items;
    awaitingTitle.clear();
    renderSidebar();
  } else if (frame.type === "tasks") {
    lastTasks = frame.items;
    renderSidebar();
  } else if (frame.type === "download") {
    // A synthetic anchor rather than location.href. Today /download/{name} answers as an attachment
    // (Content-Disposition, from FileResponse(path, filename=name) on the server), so Chromium never
    // navigates away for either approach: the live WebSocket and any running turn would survive
    // location.href too, right now. The `download` attribute is what keeps that true even if the
    // server side ever stopped setting that header: an anchor with `download` still saves rather than
    // navigates, while location.href would then load the response in place and drop both.
    const a = document.createElement("a");
    a.href = frame.url;
    a.download = frame.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } else if (frame.type === "turn_saved") {
    // A turn reached the store. Only the transcript on screen can take the control, and only if this
    // is the conversation it belongs to: a background turn's completion must not stamp a branch point
    // from another conversation onto the one being viewed.
    const active = lastConversations.find((item) => item.active);
    if (active && active.id === frame.conversation_id) {
      // Take the oldest bubble still waiting on a save in this conversation, which is the turn this
      // frame is about. A frame with nothing waiting is a turn the page never drew a bubble for (a
      // proactive run), and stamps nothing.
      const waiting = pendingTurnBubbles.findIndex((p) => p.conversationId === frame.conversation_id);
      if (waiting !== -1) {
        const [pending] = pendingTurnBubbles.splice(waiting, 1);
        stampTurnControls(pending.bubble, frame.message_index, frame.conversation_id);
      }
    }
  } else if (frame.type === "history") {
    // Also the marker that the server got past building an assistant for this connection, which is
    // what tells a later close apart from a refusal that never synced (see connect()).
    synced = true;
    // Replay a conversation (on connect or after switching), reusing the live renderers.
    log.innerHTML = "";  // replace any current transcript
    subagentCards = {};  // fresh view: drop any live sub-agent card references
    // Drop the references to the bubbles just removed. Without this they stay live but detached, and
    // the next token or thinking frame -- from any conversation -- re-attaches the old conversation's
    // partial bubble into this view and keeps writing into it. A `partial` item below re-opens the
    // streaming bubble when the conversation being replayed is the one that owns the running turn.
    cancelStreamRender();
    streamingBubble = null;
    streamingText = "";
    thinkingBlock = null;
    pendingTurnBubbles = [];  // the bubbles these referred to were just detached by the repaint
    stickToBottom = true;  // a freshly loaded conversation starts pinned to the newest message
    setProcessing(false);  // idle unless the "working" frame behind this one says otherwise
    setWorking(null);  // reset; that same frame re-shows the indicator when the turn is still running
    // The conversation this transcript belongs to. The preceding `conversations` frame has already
    // updated lastConversations by the time `history` arrives (see _sync_view), so this is the
    // conversation being switched to, not the one switched from.
    const replayConversationId = (lastConversations.find((item) => item.active) || {}).id;
    // The turn being replayed, and whether its controls have been placed. Both controls go on the
    // first bubble of the turn, so they are placed as soon as one appears rather than at the turn's
    // end, and a turn drawing several bubbles (a message with text and images) stamps only the first.
    let replayTurnIndex = -1;
    let replayTurnStamped = false;
    const stampTurnStart = (bubble) => {
      if (replayTurnStamped) return;
      stampTurnControls(bubble, replayTurnIndex, replayConversationId);
      replayTurnStamped = true;
    };
    for (const item of frame.items) {
      if (Number.isInteger(item.message_index)) {
        replayTurnIndex = item.message_index;
        replayTurnStamped = false;
      }
      if (item.type === "user") stampTurnStart(addBubble("user", item.text, item.ts));
      // An in-flight turn's answer so far: left open (and unstamped), so the rest streams into it.
      else if (item.type === "partial" && !isBlank(item.text)) {
        streamingText = item.text;
        streamingBubble = addBubble("assistant", streamingText);
      }
      else if (item.type === "plan") renderPlan(item.text);
      else if (item.type === "image") {
        const bubble = addImageBubble(item.url, item.from === "assistant" ? "assistant" : "user", item.ts);
        // An image the user uploaded can be a turn's opening bubble (an image-only message emits no
        // "user" item at all); one the assistant generated is mid-turn and never opens it.
        if (item.from !== "assistant") stampTurnStart(bubble);
      }
      else if (item.type === "thinking") {
        const f = addFoldable("thinking", { kind: "thinking" }, undefined, item.ts);
        f.body.textContent = item.text || "";
        setFoldLabel(f.label, "thinking", "", lineMetric(item.text));
      }
      else if (item.type === "tool") renderTool(item.name, item.arguments, item.ts, { response: item.response });
      else if (item.type === "loop") renderLoop(item.text, item.ts, { reason: item.reason });
      else if (item.type === "subagent") renderSubagent(item, item.ts);
      else if (item.type === "phase") renderPhase(item.label, item.detail, item.ts);
      else if (item.type === "reasoning") addMarkdownBubble("assistant", item.text, item.ts);
      else if (item.type === "message" && !isBlank(item.text)) addMarkdownBubble(item.proactive ? "proactive" : "assistant", item.text, item.ts);
      // Why a turn stopped early. A scheduled run's error never reached this conversation live, so on
      // reload this is the only account of it the conversation has.
      else if (item.type === "notice") addBubble("notice", item.text, item.ts);
    }
    autoscroll();
  }
}

// A gated tool needs confirmation: show the call and Allow/Deny buttons. The reply is a plain
// "y"/"n" frame, which the server routes to the waiting tool call (same path as typing it).
function renderApproval(name, args) {
  const el = document.createElement("div");
  el.className = "bubble approval";
  const prompt = document.createElement("div");
  prompt.className = "prompt";
  prompt.textContent = "Allow ";
  const code = document.createElement("code");
  code.textContent = toolLine(name, args);
  prompt.appendChild(code);
  prompt.appendChild(document.createTextNode("?"));
  const actions = document.createElement("div");
  actions.className = "actions";
  const answer = (reply) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(reply);
    actions.querySelectorAll("button").forEach((b) => (b.disabled = true));
  };
  const allow = document.createElement("button");
  allow.textContent = "Allow";
  allow.addEventListener("click", () => answer("y"));
  const deny = document.createElement("button");
  deny.className = "deny";
  deny.textContent = "Deny";
  deny.addEventListener("click", () => answer("n"));
  actions.appendChild(allow);
  actions.appendChild(deny);
  el.appendChild(prompt);
  el.appendChild(actions);
  appendToLog(el);
  autoscroll();
}

// Deep planning review: show Approve / Edit / Reject. Replies are plain "approve"/"reject"/"edit: <text>"
// frames the server routes to the waiting plan (same path as tool approval).
function renderPlanReview(planText, critique) {
  const el = document.createElement("div");
  el.className = "bubble approval";
  const prompt = document.createElement("div");
  prompt.className = "prompt";
  prompt.textContent = "Proceed with this plan?";
  let note = null;
  if (critique) {
    note = document.createElement("div");
    note.className = "prompt";
    note.style.opacity = "0.8";
    note.style.whiteSpace = "pre-wrap";
    note.textContent = "Reviewer's concerns:\n" + critique;
  }
  const actions = document.createElement("div");
  actions.className = "actions";
  const send = (reply) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(reply);
    el.querySelectorAll("button, textarea").forEach((b) => (b.disabled = true));
  };
  const approve = document.createElement("button");
  approve.textContent = "Approve";
  approve.addEventListener("click", () => send("approve"));
  const edit = document.createElement("button");
  edit.textContent = "Edit";
  edit.addEventListener("click", () => {
    const box = document.createElement("textarea");
    box.value = planText;
    box.rows = Math.min(16, (planText.match(/\n/g) || []).length + 3);
    box.style.width = "100%";
    const saveEdit = document.createElement("button");
    saveEdit.textContent = "Approve edited";
    saveEdit.addEventListener("click", () => send("edit: " + box.value));
    actions.replaceChildren(saveEdit);
    el.insertBefore(box, actions);
    box.focus();
  });
  const reject = document.createElement("button");
  reject.className = "deny";
  reject.textContent = "Reject";
  reject.addEventListener("click", () => send("reject"));
  actions.appendChild(approve);
  actions.appendChild(edit);
  actions.appendChild(reject);
  el.appendChild(prompt);
  if (note) el.appendChild(note);
  el.appendChild(actions);
  appendToLog(el);
  autoscroll();
}

// Reconnect after a dropped socket, so restarting Kokua does not leave a page that can only be
// reloaded. The server resyncs the whole view on every connection (conversations, history, settings,
// tasks) and the "history" frame replaces the transcript, so a reconnected page repaints rather than
// duplicating what it already showed. What a reconnect does not restore is a turn that was in flight
// when the socket dropped: the server builds an assistant per connection, so the page comes back to
// what was persisted, not to the live stream it was watching.
const RECONNECT_DELAY_MS = 500;
const RECONNECT_DELAY_MAX_MS = 10000;
// Faster than the second it counts in, so a tick lands close to each rounded boundary. On a 1000ms
// interval the label drifts against the deadline it is computed from and skips numbers.
const RECONNECT_TICK_MS = 250;
let reconnectDelay = RECONNECT_DELAY_MS;
let reconnectNotice = null;
let reconnectLabel = null;
let reconnectButton = null;
let reconnectTimeout = null;
let reconnectCountdown = null;
let reconnectAt = 0;

// The two flags below separate the ways a socket can close, because only one of them is worth
// retrying. A server that is not up yet closes having sent nothing, which is the ordinary restart
// case. A server that answered and refused us sends one message frame and closes without syncing:
// the one-connection guard's "busy in another tab", or a config error reported in place of a
// session. Retrying a refusal would thrash and bury its own message under repetitions of itself, so
// only a connection that synced, or one that never heard from a server at all, reconnects.
let synced = false;
let frameReceived = false;

// One notice for the whole outage, however many attempts it takes, so a long restart does not fill
// the transcript with its own retries. Its label is rewritten in place rather than reprinted: a
// countdown while an attempt is pending, the bare wording while one is open. At a ceiling of ten
// seconds a static "Reconnecting..." reads as a page that has given up, and the reader has no way to
// tell a wait from a hang.
function showReconnectNotice() {
  if (!reconnectNotice) {
    reconnectNotice = notice("");
    reconnectLabel = document.createElement("span");
    reconnectButton = document.createElement("button");
    reconnectButton.textContent = "Retry now";
    reconnectButton.addEventListener("click", attemptReconnect);
    // Into the body, not the bubble: appended straight to the bubble they would follow the body
    // block and break the one-line notice onto two.
    bubbleBody(reconnectNotice).append(reconnectLabel, reconnectButton);
  }
  reconnectButton.disabled = false;
}

function dropReconnectNotice() {
  clearReconnectTimers();
  if (reconnectNotice) reconnectNotice.remove();
  reconnectNotice = reconnectLabel = reconnectButton = null;
}

function clearReconnectTimers() {
  clearTimeout(reconnectTimeout);
  clearInterval(reconnectCountdown);
  reconnectTimeout = reconnectCountdown = null;
}

function scheduleReconnect() {
  showReconnectNotice();
  reconnectAt = Date.now() + reconnectDelay;
  reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_DELAY_MAX_MS);
  reconnectTimeout = setTimeout(attemptReconnect, reconnectAt - Date.now());
  reconnectCountdown = setInterval(paintReconnectLabel, RECONNECT_TICK_MS);
  paintReconnectLabel();
}

// Rounded up, because the first wait is 500ms and would otherwise spend its whole life reading
// "in 0s", which says the attempt has already been made.
function paintReconnectLabel() {
  const seconds = Math.max(0, Math.ceil((reconnectAt - Date.now()) / 1000));
  reconnectLabel.textContent = `Disconnected. Reconnecting in ${seconds}s...`;
}

// Both the timer's expiry and the button's click land here, so an impatient reader takes exactly the
// path the backoff would have. What the click deliberately does not do is reset the backoff:
// `ws.onopen` resets it, on a connection that actually succeeded, so a manual attempt that fails
// falls back onto the schedule it was already on instead of restarting at 500ms and letting a held
// mouse hammer a server that is genuinely down.
function attemptReconnect() {
  clearReconnectTimers();
  reconnectLabel.textContent = "Disconnected. Reconnecting...";
  reconnectButton.disabled = true;
  connect();
}

function connect() {
  synced = false;
  frameReceived = false;
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = handleFrame;
  ws.onopen = () => {
    reconnectDelay = RECONNECT_DELAY_MS;
    clearReconnectTimers();
    if (reconnectNotice) {
      reconnectNotice.remove();
      reconnectNotice = reconnectLabel = reconnectButton = null;
    }
    input.disabled = false;
    sendBtn.disabled = false;
    input.focus();
  };
  ws.onclose = () => {
    input.disabled = true;
    sendBtn.disabled = true;
    setProcessing(false);
    setWorking(null);
    if (frameReceived && !synced) {
      // A refusal ends the outage the retries were for, so its notice goes with them: left in place
      // it would sit above the explanation still counting down to an attempt nothing will make.
      dropReconnectNotice();
      notice("Disconnected. Reload the page to reconnect.");
      return;
    }
    scheduleReconnect();
  };
}

connect();

// Grow the box with its content up to a cap, then scroll. A one-line <input> made a multi-line
// message impossible to type at all.
const MSG_MAX_ROWS = 8;
function autoGrowInput() {
  input.style.height = "auto";
  const lineHeight = parseFloat(getComputedStyle(input).lineHeight) || 20;
  const max = lineHeight * MSG_MAX_ROWS;
  input.style.height = Math.min(input.scrollHeight, max) + "px";
  input.style.overflowY = input.scrollHeight > max ? "auto" : "hidden";
}
input.addEventListener("input", autoGrowInput);

// A textarea does not submit its form on Enter, so this is what sends. `isComposing` is the guard that
// keeps an IME's Enter (accepting a candidate) from sending a half-typed message.
input.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
  e.preventDefault();
  form.requestSubmit();
});

// Plan toggle: a sticky per-request switch. While on, each sent message is planned (drafted, then
// executed) by wrapping it as "/plan <text>" -- the same command the server already handles -- while
// the chat still shows the user's own words. Stays on across turns until clicked off.
let planNext = false;
planToggle.addEventListener("click", () => {
  planNext = !planNext;
  planToggle.classList.toggle("active", planNext);
  planToggle.setAttribute("aria-pressed", planNext ? "true" : "false");
  // A planned turn runs its own agents at the efforts their config tables declare, so the picker would
  // be a silent no-op while Plan is on. Disabled rather than hidden, so its value is still there when
  // Plan goes back off.
  thinkingLevel.disabled = planNext;
  thinkingLevel.title = planNext
    ? "Planning runs at the configured effort"
    : "Reasoning effort for the messages you send";
  input.focus();
});

// Reasoning effort for the messages that follow: sticky, like the Plan toggle beside it. "default" is
// the absence of a request, which is what leaves the configured effort in force, so it sends no field
// and no frame of its own.
function thinkingChoice() {
  // A disabled select keeps whatever it was showing, so the disabled state has to be read here too and
  // not only when it is set: Plan turning on must silence a choice made before it.
  if (thinkingLevel.disabled || thinkingLevel.value === "default") return null;
  return thinkingLevel.value;
}
function syncThinkingAccent() {
  thinkingLevel.classList.toggle("active", thinkingLevel.value !== "default");
}
thinkingLevel.addEventListener("change", () => {
  syncThinkingAccent();
  input.focus();
});
// A browser restores a <select>'s value across an ordinary reload without firing "change", so a
// reload with a non-default choice would otherwise leave the accent off while thinkingChoice() still
// returns the restored value. Called once at startup so the two can never disagree.
syncThinkingAccent();

// Images staged for the next message, each {name, dataUrl}. Read client-side, sent inline in an
// "input" frame; the server saves them to disk and hands the model the files.
let attached = [];
attachBtn.addEventListener("click", () => imageInput.click());
imageInput.addEventListener("change", () => { addFiles(imageInput.files); imageInput.value = ""; });
// Pasting an image into the message box attaches it too (screenshots, copied images).
input.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.items || [])].filter(i => i.type.startsWith("image/")).map(i => i.getAsFile());
  if (files.length) { e.preventDefault(); addFiles(files); }
});

function addFiles(fileList) {
  for (const file of fileList) {
    if (!file || !file.type.startsWith("image/")) continue;
    const reader = new FileReader();
    reader.onload = (ev) => { attached.push({ name: file.name, dataUrl: ev.target.result }); renderPreviews(); };
    reader.readAsDataURL(file);
  }
}

function renderPreviews() {
  attachPreviews.innerHTML = "";
  attachPreviews.classList.toggle("hidden", attached.length === 0);
  attached.forEach((item, i) => {
    const chip = document.createElement("div");
    chip.className = "chip";
    const img = document.createElement("img");
    img.src = item.dataUrl;
    img.alt = item.name;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove";
    remove.textContent = "×";
    remove.title = "Remove";
    remove.addEventListener("click", () => { attached.splice(i, 1); renderPreviews(); });
    chip.appendChild(img);
    chip.appendChild(remove);
    attachPreviews.appendChild(chip);
  });
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if ((!text && attached.length === 0) || ws.readyState !== WebSocket.OPEN) return;
  stickToBottom = true;  // sending snaps back to the bottom to follow the reply
  subagentCards = {};  // new turn: card ids (reviewer's plan-review-0, ... or a spawn's id) start fresh
  const now = new Date();
  // The first bubble this message draws is the turn's opening bubble, whichever it is: an image-only
  // message has no text bubble, so its control has to land on the image instead.
  let opening = text ? addBubble("user", text, now) : null;  // the user's own words, not the /plan wrapper
  for (const item of attached) {
    const bubble = addImageBubble(item.dataUrl, "user", now);  // echo attachments locally
    if (!opening) opening = bubble;
  }
  const thinking = thinkingChoice();
  // What the server will actually parse, settled before the queue below reads it, because it is not
  // always what was typed. A message carrying anything besides its text goes as an input frame, which
  // never takes the /plan wrapper: an image turn has never been plannable, and the picker is disabled
  // whenever Plan is on, so a frame and a wrapper can never both be wanted.
  // `thinking` is tested for truthiness here, the same way the frame body below decides whether to
  // set `frame.thinking`: a future reasoning-effort option with a falsy non-null value must not go as
  // a frame with no `thinking` key while also silently keeping the /plan wrapper this line exists to
  // withhold from it.
  const asFrame = attached.length > 0 || !!thinking;
  const outgoing = !asFrame && planNext && !/^\/plan(\s|$)/i.test(text) ? "/plan " + text : text;
  // Enqueuing an entry for text that turns out to run no turn is the case this predicate exists to
  // close off, the same one the queue's own declaration comment calls worse (see `pendingTurnBubbles`
  // above): nothing ever consumes that entry, so it strands at the head, and the *next* turn's
  // `turn_saved` takes the stale entry instead of its own, stamping an older bubble with a newer turn's
  // index and shifting every control after it by one turn until a repaint clears the queue. Withholding
  // an entry for a message that does run a turn fails the other way, and only once a second message is
  // already queued behind it: that turn's own `turn_saved` then consumes the *next* bubble's entry
  // instead, so a later bubble is stamped with this earlier turn's index and its control deletes more
  // than that bubble implies. This predicate cannot cause that, since it only suppresses text it can
  // positively match to a word the server reserves (`Assistant._serve_channel`), read the way it reads
  // them: /stop and /diag are matched against the whole message, the conversation commands tolerate
  // whitespace after the slash and run with or without an argument, and a workflow command with no task
  // is answered with a usage line. Everything else is enqueued, including an unrecognized "/"-word,
  // which the server deliberately runs as an ordinary turn so that "/usr/local/bin is missing" reaches
  // the model.
  //
  // Two cases stay wrong and cannot be decided here, both in the stranding direction above rather than
  // the destructive one. A "/word" naming a workflow command that an installed toolset offers but the
  // entry agent does not declare is answered with no turn, and the page has no way to know which words
  // those are. So is a composer message sent while a tool call is waiting on approval:
  // `HumanGate.resolve_reply` consumes it and no turn runs, in the approval-reply branch of
  // `Assistant._serve_channel`, but the page leaves the composer live during an approval, so typing
  // "y" strands an entry the same way. Each shifts later controls until the next repaint.
  const answeredAsCommand =
    /^\/(stop|diag)$/i.test(outgoing) ||
    /^\/\s*(new|conversations|switch)(\s|$)/i.test(outgoing) ||
    /^\/\s*plan\s*$/i.test(outgoing);
  if (opening && !answeredAsCommand) {
    const active = lastConversations.find((item) => item.active);
    if (active) pendingTurnBubbles.push({ bubble: opening, conversationId: active.id });
  }
  if (asFrame) {
    const frame = { type: "input", text };
    if (attached.length) frame.images = attached.map(a => a.dataUrl);
    if (thinking) frame.thinking = thinking;
    ws.send(JSON.stringify(frame));
    if (attached.length) {
      attached = [];
      renderPreviews();
    }
  } else {
    ws.send(outgoing);
  }
  input.value = "";
  autoGrowInput();  // collapse the box back to one row
  setProcessing(true);  // a turn is now being processed; allow stopping it
  setWorking(0);  // and say so in the transcript, until the first frame of the reply displaces it
});

// Cancel the in-flight reply. Disabled unless a turn is processing; disable on click so it
// can't be sent twice (the server's "(stopped)" message also clears the processing state).
stopBtn.addEventListener("click", () => {
  if (ws.readyState === WebSocket.OPEN) ws.send("/stop");
  setProcessing(false);
  setWorking(null);  // nothing is coming that would clear it: the server answers a stop with a message
});
