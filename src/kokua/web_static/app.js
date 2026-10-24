const log = document.getElementById("log");
const form = document.getElementById("form");
const input = document.getElementById("msg");
const sendBtn = document.getElementById("send");
const stopBtn = document.getElementById("stop");
const planToggle = document.getElementById("plan-toggle");
const roundPanel = document.getElementById("round-panel");
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
const alertLayer = document.getElementById("alerts");
const convHeading = document.getElementById("conv-heading");
const modelLabel = document.getElementById("model-label");

// Alert cards ("notification" frames): work the user is not watching, raised over the page instead of
// written into the transcript, because none of it is about the conversation on screen. Three things
// produce one today (a scheduled task's report, an authorization an MCP connect is waiting on, and a
// gated tool a backgrounded turn was denied), and each is a pointer rather than a place to act: the
// deciding happens in the conversation, or in the browser tab the link opens.
//
// Newest first, and nothing expires. A card leaves when it is dismissed, or when a later card
// supersedes it: same `group`, one card. That is what keeps a task firing every ten minutes overnight
// from greeting the user with a wall of its own history.
let alerts = [];

function raiseAlert(frame) {
  if (frame.group != null) alerts = alerts.filter((a) => a.group !== frame.group);
  alerts.unshift({
    text: frame.text || "",
    ts: frame.ts,
    conversationId: frame.conversation_id || null,
    url: frame.url || null,
    group: frame.group != null ? frame.group : null,
  });
  renderAlerts();
}

// Only http(s) becomes a link. The authorization URL comes from a remote MCP server's own metadata, so
// it is not ours to trust: a `javascript:` href here would be that server scripting the page. An
// address that fails this still reaches the user, in the card's text, which always carries it in words.
function safeHref(url) {
  try {
    const parsed = new URL(url, window.location.href);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
  } catch {
    return null;
  }
}

// The title of a conversation still in the sidebar, or null once it is gone. A task prunes its older
// runs, so a card can outlive the conversation it points at; the card then keeps its text and drops
// the control, rather than offering a switch that would land on nothing.
function liveConversationTitle(id) {
  const found = lastConversations.find((conv) => conv.id === id);
  return found ? found.title : null;
}

function renderAlerts() {
  alertLayer.replaceChildren();
  // Above the cards rather than below them: the stack scrolls once it is tall enough, and the one
  // control that empties it should not be the thing you have to scroll to reach.
  if (alerts.length > 1) {
    const clear = document.createElement("button");
    clear.type = "button";
    clear.id = "alerts-clear";
    clear.className = "secondary";
    clear.textContent = `Clear all (${alerts.length})`;
    clear.addEventListener("click", () => {
      alerts = [];
      renderAlerts();
    });
    alertLayer.appendChild(clear);
  }
  alerts.forEach((alert, index) => {
    // No `role="alert"` on the card: the layer itself is the live region (`aria-live="assertive"` on
    // #alerts), and a live region nested inside one is announced twice by some screen readers.
    const card = document.createElement("div");
    card.className = "alert-card";

    const text = document.createElement("div");
    text.className = "alert-text";
    text.textContent = alert.text;  // never markdown: a card is chrome, and its text is not the model's
    card.appendChild(text);

    const foot = document.createElement("div");
    foot.className = "alert-foot";
    const when = tsParts(alert.ts);
    if (when) {
      const stamp = document.createElement("span");
      stamp.className = "alert-ts";
      stamp.textContent = when.label;
      stamp.title = when.full;
      foot.appendChild(stamp);
    }

    const title = alert.conversationId ? liveConversationTitle(alert.conversationId) : null;
    if (title !== null) {
      const open = document.createElement("button");
      open.type = "button";
      open.className = "alert-open secondary";
      open.textContent = `Open "${title}"`;
      open.addEventListener("click", () => {
        selectConversation(alert.conversationId);
        dismissAlert(index);
      });
      foot.appendChild(open);
    }

    const href = alert.url ? safeHref(alert.url) : null;
    if (href) {
      const link = document.createElement("a");
      link.className = "alert-link";
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "Authorize";
      foot.appendChild(link);
    }

    const close = document.createElement("button");
    close.type = "button";
    close.className = "alert-close secondary";
    close.textContent = "✕";
    close.setAttribute("aria-label", "Dismiss");
    close.addEventListener("click", () => dismissAlert(index));
    foot.appendChild(close);

    card.appendChild(foot);
    alertLayer.appendChild(card);
  });
}

function dismissAlert(index) {
  alerts.splice(index, 1);
  renderAlerts();
}

// Escape takes the newest card, which is the one on top. Keyed off `defaultPrevented` rather than off
// which element is focused: the composer is autofocused on load and holds focus for most of a session,
// so a rule like "only from the body" would make the shortcut unreachable exactly when it is wanted.
// Anything with a closer claim on the key (a rename input, mid-edit) calls preventDefault, and its
// listener on the input runs before this one on the document.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || e.defaultPrevented) return;
  if (alerts.length) dismissAlert(0);
  else if (shownRound) closeRoundPanel(true);
});

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
// Declared here, ahead of appendToLog, because every append consults them (see adoptRoundButton) and
// a `const` read before its line has run throws: each container's (the log's, or a card body's) most
// recent round, and what counts as a dispatch result.
const latestRound = new WeakMap();
const DISPATCH_RESULT = ".bubble.tool, .bubble.subagent, .bubble.image";

function appendToLog(el, parent) {
  if (parent) parent.appendChild(el);
  else if (workingElement) log.insertBefore(el, workingElement);
  else log.appendChild(el);
  adoptRoundButton(el, parent || log);
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
// Bubbles whose fate the server has not reported yet, keyed by the token the page minted for each.
// Keyed rather than ordered because a message has two frames it can be named by in sequence, not one:
// `inbox` first if it is accepted into the turn already running, and then either nothing more (the
// run reads it and it stays folded into that turn) or a `turn_saved` of its own after all (the run
// never reads it, so it becomes a follow-up turn, carrying the same token). The page cannot know which
// of those it sends toward. A positional queue could not tell any of this apart, so a message joining
// a running turn stranded an entry at the head and the *next* turn's save took the stale entry,
// stamping an older bubble with a newer turn's index. That is worse than losing the control, because
// the index is a valid turn boundary the server will honour, so the user would click delete on one
// message and lose that one and the message before it. See the `turn_saved` and `inbox` frame
// handling below for how the sequence actually plays out.
//
// Matching also makes an unclaimed entry harmless, where a count had to be kept honest: text the server
// answers as a command (or as the reply to a pending approval) runs no turn and is simply never claimed,
// and its entry is dropped at the next repaint along with the bubble it points at.
let pendingBubbles = new Map();
let bubbleTokens = 0;
// Tokens are unique per page load, not merely per message, because a turn outlives the page that
// started it: reload while one is running and its `turn_saved` still arrives, so a counter starting
// over at 1 would let that frame name a bubble this load drew and stamp it with the older turn's
// index, which is the mis-stamp this whole mechanism exists to prevent. Math.random rather than
// crypto.randomUUID, which is only defined in a secure context and Kokua is served over plain http.
// Scaled to a whole number and zero-padded rather than sliced out of the fraction's digits, because
// `Math.random().toString(36)` is as short as the value is round: 0.5 is "0.i", so slicing six
// characters from position 2 yields a one-character epoch, and 0 yields an empty one. Padding the
// number instead makes the width a property of the construction rather than of the draw.
const bubbleEpoch = Math.floor(Math.random() * 36 ** 6).toString(36).padStart(6, "0");

// Take the bubble this token was minted for, if it is still waiting. Deleted on the way out, so one
// token can claim one bubble: a frame arriving twice, or two frames carrying the same token, marks
// nothing the second time rather than stamping a bubble that has already been accounted for.
function claimBubble(token) {
  const bubble = pendingBubbles.get(token);
  if (bubble) pendingBubbles.delete(token);
  return bubble || null;
}

// Render/update a sub-agent card as a foldable block. Two producers share this frame type: a
// planning reviewer (role + status + issues) and a spawned sub-agent (role + task + status, its
// body built up from `append` frames). A spawn card is the one carrying `task`. A spawn made by
// another spawn names it in `parent` (see core/subagents.py's `current_spawn`) and is drawn inside
// that card, at the point in its run where the call was made; a parent this view never drew (the turn
// began before the page loaded) leaves it at the top level rather than losing it.
function renderSubagent(frame, ts) {
  let card = frame.id ? subagentCards[frame.id] : null;
  if (!card) {
    const spawn = !!frame.task;
    const parentCard = frame.parent ? subagentCards[frame.parent] : null;
    if (parentCard) {
      // The child card is something appended after the parent's open blocks, so it closes them, the
      // same way a tool entry does in appendSubagentEntry.
      parentCard.reasoning = null;
      parentCard.answer = null;
    }
    const opts = { md: true, parent: parentCard ? parentCard.body : undefined };
    card = addFoldable("subagent" + (spawn ? " spawn" : ""), {}, opts, ts);  // stamp once, when first created
    if (frame.id) subagentCards[frame.id] = card;
    card.spawn = spawn;
    card.tool = frame.tool || SPAWN_TOOL_NAME;  // only the create frame says, so it is kept on the card
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
    if (card.spawn) setFoldLabel(card.label, "subagent", card.tool + "(" + role + ")", status);
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

// The tool whose block the server suppresses in favour of the card (see SPAWN_TOOL_NAMES in
// core/transcripts.py); the card names it so the transcript still says which call was made. A card
// opened by any other spawn tool (compose_subagent) names its tool in `frame.tool`.
const SPAWN_TOOL_NAME = "spawn_subagent";

// A spawn card's first body line: the call's arguments, rendered exactly as a tool block's body
// is. A card carrying `arguments` (compose_subagent's, see core/subagents.py's `spawn_call`) shows
// them as given. Otherwise `agent_type` is reconstructed from the frame's role because kokua always
// builds AIMU's typed spawn tool, whose parameters are agent_type and task (see core/agents.py's
// build_agent_specs); AIMU's untyped mode, which kokua never builds, would take task alone.
function spawnArgsLine(frame) {
  const el = document.createElement("div");
  el.className = "sa-args";
  el.textContent = toolArgs(frame.arguments || { agent_type: frame.role || "subagent", task: frame.task });
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
  // Both can land after the card's terminal status (frames are scheduled tasks), and are drawn anyway.
  if (entry.kind === "request") {
    card.answer = null;
    renderRequest(entry, { card });
    return;
  }
  if (entry.kind === "ending") {
    card.answer = null;
    renderEnding(entry, { card, parent: card.body });
    return;
  }
  if (entry.kind === "loop") {
    card.answer = null;
    renderLoop(entry.text, undefined, { parent: card.body, reason: entry.reason });
    return;
  }
  if (entry.kind === "message") {
    // Its own branch rather than `loop`'s: `loop` is the agent loop injecting a round of its own,
    // and this is somebody's own words (the user's, or one of the turn's own agents') reaching a
    // worker already running. Filing it under `loop` would show the card crediting the loop with
    // what a person, or another agent, said. `cardMessageFrom` reads the attribution straight off
    // `entry.text` rather than a field of its own: the card carries none (principle 1 again), but
    // the text is already there, so a card row can draw the same dimmed treatment the main log's
    // `inbox.agent` gets instead of reading as a one-state row forever.
    card.answer = null;
    renderMidTurn(entry.text, undefined, { parent: card.body, from: cardMessageFrom(entry.text) });
    return;
  }
  if (entry.kind === "tool") {
    card.answer = null;
    renderTool(entry.name, entry.arguments, undefined, {
      parent: card.body,
      response: entry.response,
      responseRef: entry.response_ref,
      responseBytes: entry.response_bytes,
    });
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

// A response_bytes count as "N KB"/"N MB" for the expand control's label. A payload file only
// exists past RESPONSE_PREVIEW_CHARS (core/subagents.py), so this never needs to say "bytes".
function formatPayloadSize(bytes) {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

// What a tool call returned, as its own nested foldable below the arguments, so the arguments stay
// scannable when the card is opened. Populated on first expand, and clamped to OUTPUT_CLAMP with a
// button for the rest. Plain text only, never markdown: a tool result is untrusted input.
//
// A sub-agent's response can also carry `responseRef`/`responseBytes` (see core/subagents.py):
// `response` is then already clamped to OUTPUT_CLAMP server-side, spilled there rather than merely
// truncated for display, so the "show all" button below never applies to it. Its own control fetches
// the payload on demand instead, once, and leaves the preview in place if the fetch fails, since the
// file is never garbage collected but a user can clear the folder by hand.
function renderToolOutput(parent, response, opts) {
  const size = response.length.toLocaleString();
  const responseRef = opts && opts.responseRef;
  const responseBytes = opts && opts.responseBytes;
  // Not also `.tool`: that would make `.bubble.tool` match a card and its own output. The monospace
  // type and colour are inherited properties, so they arrive from the enclosing card regardless.
  addFoldable("tool-output", { kind: "output", metric: `${size} chars` }, {
    parent,
    onFirstExpand: (body) => {
      const text = document.createElement("span");
      text.className = "output-text";
      text.textContent = response.slice(0, OUTPUT_CLAMP);
      body.appendChild(text);
      if (responseRef) {
        const expand = document.createElement("button");
        expand.type = "button";
        expand.className = "output-more";
        expand.textContent = `Show full response (${formatPayloadSize(responseBytes)})`;
        let fetched = false;
        expand.addEventListener("click", () => {
          if (fetched) return;
          fetched = true;
          expand.disabled = true;
          fetch(responseRef)
            .then((r) => { if (!r.ok) throw new Error(String(r.status)); return r.text(); })
            .then((full) => {
              text.textContent = full;
              expand.remove();
            })
            .catch(() => {
              // The preview already on screen stands; only the control changes, to a note rather
              // than a second button, since retrying would hit the same missing or unreadable file.
              expand.textContent = "could not load full response";
              expand.disabled = true;
              expand.classList.add("output-error");
            });
        });
        body.appendChild(expand);
        return;
      }
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

// --- What the model saw -------------------------------------------------------------------
// One `{ }` button per model call, on the round's own output, opening a right-hand panel with the round's
// two halves: the request the model was sent and the response it returned (a small file, fetched when the
// round is shown). A single request can be 120 KB at 30k tokens
// of context and megabytes once images ride along (base64, repeated on every call), so the viewer
// costs the page only what the reader opens: the summary line is drawn from the call record with no
// fetch, the tree is fetched when the round is shown and rendered a level at a time, long strings are cut,
// and above a ceiling there is no inline tree at all, only the download.
const REQUEST_INLINE_CEILING = 2_000_000;  // characters; a request above this is download-only
const JSON_STRING_CLAMP = 4000;  // characters of a string shown before "show all"
const IMAGE_DATA_MIN = 1000;  // a string this long that is a data:image/ URL or all base64 is image data
// The only reference shape the server hands out (payloads.save_text's name), checked so a stored
// record cannot point the fetch or the download link anywhere else.
const PAYLOAD_REF = /^\/payloads\/[0-9a-f]{64}$/;
const BASE64_ONLY = /^[A-Za-z0-9+/=]+$/;

// Every surface counts rounds from 1: buttons, endings, and the export. AIMU's own counter starts at 0.
function requestSummary(call) {
  const parts = [];
  if (call.message_count != null) parts.push(`${call.message_count} messages`);
  if (call.tool_count != null) parts.push(`${call.tool_count} tools`);
  if (call.payload_chars != null) parts.push(formatPayloadSize(call.payload_chars));
  if (call.stop_reason) parts.push(call.stop_reason);
  if (call.error) parts.push(call.error);
  return parts.join(" · ");
}

// The number of the turn a block belongs to, read from the nearest user bubble above it (the turn's
// own opening bubble, which carries `data-turn` once the page has learned it from history or from
// `turn_saved`). Read when the panel opens rather than when it is drawn, since a live turn is
// numbered only when it is saved. Null when unknown, which drops the `turn-N-` prefix from a download.
function enclosingTurn(el) {
  let node = el;
  while (node && node.parentElement !== log) node = node.parentElement;
  for (node = node && node.previousElementSibling; node; node = node.previousElementSibling) {
    if (node.matches(".bubble.user:not(.mid-turn)")) return node.dataset.turn || null;
  }
  return null;
}

// Only characters the download-name allowlist on the server accepts, so the name survives the route.
function downloadSlug(text) {
  return String(text).replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
}

// One section of a round (its request, or its response): a title, a download link when the file was
// captured, and a container the caller fills. `empty` writes a one-line explanation into it instead.
function roundSection(body, cls, title, ref, downloadName, downloadClass) {
  const section = document.createElement("div");
  section.className = "round-section " + cls;
  const heading = document.createElement("div");
  heading.className = "round-section-title";
  heading.textContent = title;
  section.appendChild(heading);
  if (ref) {
    const link = document.createElement("a");
    link.className = downloadClass;
    link.href = ref + "?download=" + downloadName;
    link.download = downloadName;
    link.textContent = "download";
    section.appendChild(link);
  }
  const content = document.createElement("div");
  section.appendChild(content);
  body.appendChild(section);
  const empty = (text) => {
    const line = document.createElement("div");
    line.className = "request-empty";
    line.textContent = text;
    content.appendChild(line);
  };
  return { content, empty };
}

// What the model returned for this round, from the recorded response file: its reasoning, the tool
// calls it asked for (each name with its arguments as a tree), and its text. A round that only asked
// for tools has no text, which is exactly why the calls are shown here rather than left to the tool
// cards elsewhere in the turn.
function renderResponse(response, parent) {
  let shown = false;
  if (!response || typeof response !== "object") response = {};
  if (typeof response.thinking === "string" && response.thinking) {
    const block = document.createElement("div");
    block.className = "response-thinking";
    renderJsonString(response.thinking, block);
    parent.appendChild(block);
    shown = true;
  }
  for (const call of Array.isArray(response.tool_calls) ? response.tool_calls : []) {
    const block = document.createElement("div");
    block.className = "response-tool";
    const name = document.createElement("div");
    name.className = "response-tool-name";
    name.textContent = call && call.name != null ? String(call.name) : "(unnamed tool)";
    block.appendChild(name);
    if (call && call.arguments != null) renderJsonNode(call.arguments, block, 0);
    parent.appendChild(block);
    shown = true;
  }
  if (typeof response.text === "string" && response.text) {
    const block = document.createElement("div");
    block.className = "response-text";
    renderJsonString(response.text, block);
    parent.appendChild(block);
    shown = true;
  }
  if (!shown) {
    const line = document.createElement("div");
    line.className = "request-empty";
    line.textContent = "the model returned nothing";
    parent.appendChild(line);
  }
}

// One round: the request the model received and the response it returned, drawn in the round panel.
// Nothing here runs until a round is shown, which is what keeps the fetches lazy.
function renderRoundDetails(record) {
  const call = record.call;
  const round = record.round;
  const ref = PAYLOAD_REF.test(call.payload_ref || "") ? call.payload_ref : null;
  const responseRef = PAYLOAD_REF.test(call.response_ref || "") ? call.response_ref : null;
  roundPanel.replaceChildren();

  const header = document.createElement("div");
  header.className = "round-panel-header";
  const turn = enclosingTurn(record.output);
  const title = document.createElement("span");
  title.className = "round-panel-title";
  title.textContent = (/^\d+$/.test(turn || "") ? `Turn ${turn} · ` : "") + `round ${round}`;
  const agent = document.createElement("span");
  agent.className = "round-panel-agent";
  agent.textContent = record.agent || "";
  const close = document.createElement("button");
  close.type = "button";
  close.className = "round-panel-close";
  close.textContent = "×";
  close.title = "Close";
  close.setAttribute("aria-label", "Close");
  close.addEventListener("click", () => closeRoundPanel(true));
  header.append(title, agent, close);
  roundPanel.appendChild(header);

  const summary = document.createElement("div");
  summary.className = "round-summary";
  summary.textContent = requestSummary(call);
  roundPanel.appendChild(summary);

  const note = document.createElement("div");
  note.className = "request-note";
  note.textContent = call.endpoint
    ? [call.endpoint, call.model].filter(Boolean).join(" · ")
    : "nearest equivalent: no wire request; inspectable, not replayable";
  roundPanel.appendChild(note);

  if (record.ending) roundPanel.appendChild(roundEndingLine(record.ending));

  const slug = call.agent ? downloadSlug(call.agent) : "";
  const stem = (/^\d+$/.test(turn || "") ? `turn-${turn}-` : "") + `round-${round}` + (slug ? "-" + slug : "");

  const request = roundSection(roundPanel, "round-request", "Request", ref, stem + "-request.json", "request-download");
  request.content.className = "request-tree";
  if (!ref) request.empty("not captured");
  else if (call.payload_chars > REQUEST_INLINE_CEILING) request.empty("too large to show here; download it");
  else {
    fetch(ref)
      .then((r) => { if (!r.ok) throw new Error(String(r.status)); return r.text(); })
      .then((text) => { if (shownRound === record) renderJsonNode(JSON.parse(text), request.content, 0); })
      .catch(() => { if (shownRound === record) request.empty("could not load the request"); });
  }

  const reply = roundSection(roundPanel, "round-response", "Response", responseRef, stem + "-response.json", "response-download");
  if (!responseRef) reply.empty("not captured");
  else {
    fetch(responseRef)
      .then((r) => { if (!r.ok) throw new Error(String(r.status)); return r.text(); })
      .then((text) => { if (shownRound === record) renderResponse(JSON.parse(text), reply.content); })
      .catch(() => { if (shownRound === record) reply.empty("could not load the response"); });
  }
}

function roundEndingLine(ending) {
  const line = document.createElement("div");
  line.className = "round-ending";
  line.textContent = endingText(ending);
  return line;
}

// The round whose details the panel shows, or null while it is closed. Closed on every page load and
// on every history replay, since the log it pointed into is rebuilt.
let shownRound = null;
// The top-level log's rounds, oldest first. A sub-agent card keeps its own list on the card.
let topRounds = [];

// `refocus` hands keyboard focus back to the round's button, for a close from inside the panel (its close
// button, or Escape) where focus would otherwise be left on an element that just went away.
function closeRoundPanel(refocus) {
  const closing = shownRound;
  if (closing) {
    closing.output.classList.remove("round-selected");
    closing.button.setAttribute("aria-expanded", "false");
  }
  shownRound = null;
  roundPanel.hidden = true;
  roundPanel.replaceChildren();
  document.body.classList.remove("round-panel-open");
  if (refocus && closing && closing.button.isConnected) closing.button.focus();
}

function openRoundPanel(record) {
  if (shownRound) {
    shownRound.output.classList.remove("round-selected");
    shownRound.button.setAttribute("aria-expanded", "false");
  }
  shownRound = record;
  record.output.classList.add("round-selected");
  record.button.setAttribute("aria-expanded", "true");
  roundPanel.hidden = false;
  document.body.classList.add("round-panel-open");
  renderRoundDetails(record);
  roundPanel.scrollTop = 0;
}

// What a round button may sit on: the model's own output. A user's bubble, a mid-turn message, the
// loop's injected-round marker, a phase divider, and the page's notices are not it.
const ROUND_OUTPUT = ".bubble:not(.user, .phase, .notice, .approval, .inbox, .loop, .mid-turn)";

// Blocks that can sit between a round's output and its request without being either: a message the user
// typed into the running turn (`.sent-mid-turn` is added when the page draws it, since the server's
// `.mid-turn` mark only arrives later), the injected-round marker, a delivered note, and the page's own
// notices and approval prompts. Anything else that is not output (a turn-opening user bubble, a phase
// divider) ends the search.
const ROUND_PASSTHROUGH = ".mid-turn, .sent-mid-turn, .inbox, .loop, .notice, .approval";

// The model's own output a request describes: the nearest one above the end of `container`, looking past
// ROUND_PASSTHROUGH blocks. Null when a turn boundary comes first (the round produced nothing).
function roundOutputIn(container) {
  let el = container === log ? lastContentElement() : container.lastElementChild;
  while (el && el.matches(ROUND_PASSTHROUGH)) el = el.previousElementSibling;
  return el && el.matches(ROUND_OUTPUT) ? el : null;
}

// The button's tooltip and accessible name: the turn it belongs to and its round, or the round alone
// while the turn is not yet numbered.
function labelRoundButton(button, record, output) {
  const turn = output ? enclosingTurn(output) : null;
  const label = (/^\d+$/.test(turn || "") ? `turn ${turn}, ` : "") + `round ${record.round}`;
  button.title = label;
  button.setAttribute("aria-label", label);
}

// One round's button, on the output its request arrived after. A round that produced none (it failed
// first, or only the user's bubble precedes it) gets a small row of its own holding the button.
function addRound(call, container, rounds, agent) {
  let output = roundOutputIn(container);
  const record = { call, round: (Number.isInteger(call.round) ? call.round : 0) + 1, ending: null, agent };
  const button = document.createElement("button");
  button.type = "button";
  button.className = "round-button";
  button.textContent = "{ }";
  labelRoundButton(button, record, output);
  button.dataset.round = String(record.round);
  button.setAttribute("aria-controls", "round-panel");
  button.setAttribute("aria-expanded", "false");
  // The turn is numbered only once it is saved, so a live button is drawn before its turn is known;
  // the label is recomputed when someone is about to read it.
  const relabel = () => labelRoundButton(button, record, record.output);
  button.addEventListener("mouseenter", relabel);
  button.addEventListener("focus", relabel);
  button.addEventListener("click", () => {
    if (shownRound === record) closeRoundPanel();
    else openRoundPanel(record);
  });
  record.button = button;
  placeRoundButton(record, output, container);
  latestRound.set(container, record);
  rounds.push(record);
  autoscroll();
  return record;
}

// Put a round's button on `output`, or, with no output, in a small row of its own in `container`.
function placeRoundButton(record, output, container) {
  const button = record.button;
  if (output && output.classList.contains("foldable")) {
    // A foldable's header is itself the <button> that folds it, and a button cannot hold another, so
    // the round button sits in a slot pinned over the header's right end instead (see .round-slot);
    // the header reserves room for however many buttons the slot holds.
    let slot = output.querySelector(":scope > .round-slot");
    if (!slot) {
      slot = document.createElement("div");
      slot.className = "round-slot";
      output.appendChild(slot);
    }
    // Info, buttons, timestamp: the caption leaves the header (where it would sit left of the buttons)
    // and rides the end of the slot, after the buttons, at the same right edge it had.
    const caption = output.querySelector(":scope > .fold-header > .fold-ts");
    if (caption) slot.appendChild(caption);
    slot.insertBefore(button, slot.querySelector(".fold-ts"));
    output.style.setProperty("--round-buttons", String(slot.children.length));
  } else if (output) bubbleMetaRow(output).appendChild(button);
  else {
    output = document.createElement("div");
    output.className = "round-orphan";
    output.appendChild(button);
    appendToLog(output, container === log ? undefined : container);
  }
  record.output = output;
}

// Take a round's button off the output holding it, undoing what placeRoundButton did there: an orphan
// row goes, and a foldable whose slot is left with no button gets its caption back in its header.
function releaseRoundButton(record) {
  const held = record.output;
  record.button.remove();
  if (!held) return;
  held.classList.remove("round-selected");
  if (held.classList.contains("round-orphan")) {
    held.remove();
    return;
  }
  const slot = held.querySelector(":scope > .round-slot");
  if (!slot) return;
  if (slot.querySelector(".round-button")) {
    held.style.setProperty("--round-buttons", String(slot.children.length));
    return;
  }
  const caption = slot.querySelector(".fold-ts");
  const header = held.querySelector(":scope > .fold-header");
  if (caption && header) header.appendChild(caption);
  slot.remove();
  held.style.removeProperty("--round-buttons");
}

// A dispatch result (a tool card, a sub-agent card, a generated image) is the last thing its round
// produced, but it reaches the page after that round's request: a round's record completes when the
// model's response ends, and the tool it asked for runs afterwards. A live tool round's button is
// therefore drawn first on whatever came before (the round's reasoning, or a row of its own), and a
// sub-agent card, whose entries are stored in that same arrival order, does the same on reload. So when a
// dispatch result lands right after the latest round's output (looking past ROUND_PASSTHROUGH), the
// button moves onto it: the output a top-level replay, which orders the tool card first, puts it on.
// Model output (an answer, reasoning) never takes it, since that belongs to the round that comes next.
function adoptRoundButton(el, container) {
  if (!el.matches(DISPATCH_RESULT)) return;
  const record = latestRound.get(container);
  if (!record || !record.output) return;
  // Only a round whose response asked for tools has dispatch results. Without this, a card a reload
  // places at a turn's end (one matched to no call, such as a plan reviewer's) would take the button of
  // a round that only answered. A record with no count (written before it was recorded) is not moved.
  if (!(record.call.tool_call_count > 0)) return;
  let previous = el.previousElementSibling;
  while (previous && previous.matches(ROUND_PASSTHROUGH)) previous = previous.previousElementSibling;
  if (previous !== record.output) return;
  const selected = shownRound === record;
  releaseRoundButton(record);
  placeRoundButton(record, el, container);
  if (selected) el.classList.add("round-selected");
}

function renderRequest(call, opts) {
  const card = opts && opts.card;
  if (card) {
    card.rounds = card.rounds || [];
    return addRound(call, card.body, card.rounds, call.agent || card.role);
  }
  return addRound(call, log, topRounds, call.agent);
}

// An ending belongs to the latest round of its run, so its wording and its warning tint ride that
// round's button instead of a line in the log. With no such round (capture always records calls, so
// this should be rare) the old inline line stands, so the information is never lost.
function renderEnding(ending, opts) {
  const card = opts && opts.card;
  let target = null;
  if (card) target = card.rounds && card.rounds.length ? card.rounds[card.rounds.length - 1] : null;
  else target = [...topRounds].reverse().find((r) => r.call.run_id === ending.run_id) || null;
  if (!target) return renderEndingLine(ending, opts);
  target.ending = ending;
  if (ending.ended_by === "wrap_up" || ending.ended_by === "error") target.button.classList.add("warn");
  if (shownRound === target) {
    const line = roundEndingLine(ending);
    const existing = roundPanel.querySelector(".round-ending");
    if (existing) existing.replaceWith(line);
    else roundPanel.insertBefore(line, roundPanel.querySelector(".round-request"));
  }
  return target;
}

// How a tool loop ended, worded as the terminal and the Markdown export word it. A forced wrap-up
// names the cap rather than a round.
function endingText(ending) {
  const limit = ending.max_iterations;
  const ofLimit = limit != null ? ` of ${limit}` : "";
  const position = `round ${ending.round + 1}${ofLimit}`;
  if (ending.ended_by === "answered") return `ended: answered at ${position}`;
  if (ending.ended_by === "wrap_up") {
    const cap = limit != null ? `limit of ${limit} rounds` : "limit";
    return `ended: tool-use ${cap} reached; answered on a forced wrap-up call`;
  }
  if (ending.ended_by === "error") return `ended: error${ending.error ? ` (${ending.error})` : ""} at ${position}`;
  return `ended at ${position}`;
}

function renderEndingLine(ending, opts) {
  const el = document.createElement("div");
  el.className = "ending";
  el.textContent = endingText(ending);
  if (ending.agent) el.title = ending.agent;
  appendToLog(el, opts && opts.parent);
  autoscroll();
  return el;
}

function jsonSizeHint(length) {
  return length >= 1000 ? `${Math.round(length / 1000)}k chars` : `${length} chars`;
}

function jsonKey(parent, key, hint) {
  const span = document.createElement("span");
  span.className = "json-key";
  span.textContent = key == null ? "" : String(key);
  parent.appendChild(span);
  if (hint) {
    const size = document.createElement("span");
    size.className = "json-size";
    size.textContent = " " + hint;
    parent.appendChild(size);
  }
}

function isImageData(text) {
  if (text.length < IMAGE_DATA_MIN) return false;
  if (text.startsWith("data:image/")) return true;
  // Bare base64 has no marker of its own, and "xxxx..." is valid base64 too, so a run of a few
  // distinct characters is prose-like or padding, not an encoded image.
  return BASE64_ONLY.test(text) && new Set(text.slice(0, 200)).size >= 16;
}

function renderJsonString(text, parent) {
  const image = isImageData(text);
  const el = document.createElement(image ? "div" : "pre");
  el.className = image ? "json-image" : "json-string";
  const clamped = text.length > JSON_STRING_CLAMP;
  el.textContent = image ? `image data, ${formatPayloadSize(text.length)}` : text.slice(0, JSON_STRING_CLAMP);
  parent.appendChild(el);
  if (!clamped && !image) return;
  const more = document.createElement("button");
  more.type = "button";
  more.className = "json-more";
  more.textContent = `show all (${text.length} chars)`;
  more.addEventListener("click", () => {
    el.textContent = text;
    el.classList.add("json-full");
    more.remove();
  });
  parent.appendChild(more);
}

// A parsed request as a tree, a level at a time. An object or array is a <details> whose children are
// built on its first `toggle` open, so an unopened subtree costs one element however much it holds;
// depth 0 (the request itself) lists its own children at once, the summary the reader asked for.
// Everything from the payload reaches the DOM through textContent: it is model and user content.
function renderJsonNode(value, parent, depth, key) {
  if (value !== null && typeof value === "object") {
    const entries = Array.isArray(value)
      ? value.map((item, index) => [index, item])
      : Object.entries(value);
    if (depth === 0) {
      for (const [childKey, child] of entries) renderJsonNode(child, parent, 1, childKey);
      return;
    }
    const node = document.createElement("details");
    node.className = "json-node";
    const summary = document.createElement("summary");
    jsonKey(summary, key, Array.isArray(value) ? `[${entries.length}]` : `{${entries.length}}`);
    node.appendChild(summary);
    let filled = false;
    node.addEventListener("toggle", () => {
      if (!node.open || filled) return;
      filled = true;
      const children = document.createElement("div");
      children.className = "json-children";
      node.appendChild(children);
      for (const [childKey, child] of entries) renderJsonNode(child, children, depth + 1, childKey);
    });
    parent.appendChild(node);
    return;
  }
  const row = document.createElement("div");
  row.className = "json-leaf";
  const isString = typeof value === "string";
  jsonKey(row, key, isString && value.length > JSON_STRING_CLAMP ? `(${jsonSizeHint(value.length)})` : "");
  if (isString) renderJsonString(value, row);
  else {
    const scalar = document.createElement("span");
    scalar.className = "json-scalar";
    scalar.textContent = String(value);
    row.appendChild(scalar);
  }
  parent.appendChild(row);
}

// A turn's number on its opening bubble, in the meta row beside the datetime and turn controls.
function addTurnBadge(bubble, turn) {
  if (!Number.isInteger(turn) || bubble.dataset.turn) return;
  bubble.dataset.turn = String(turn);
  const badge = document.createElement("span");
  badge.className = "turn-badge";
  badge.textContent = `turn ${turn}`;
  bubbleMetaRow(bubble).appendChild(badge);
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
  // A spilled response's `response` is already clamped to the preview cap, so its true size is
  // `responseBytes` (see core/subagents.py); without this, a multi-megabyte result reads as
  // "4,000 chars" in the collapsed row and the truth only appears once the card is opened and
  // expanded.
  const metric = returned ? `${(opts.responseBytes ?? response.length).toLocaleString()} chars` : "";
  const parts = { kind: "tool", payload: toolLine(name, args), metric };
  const f = addFoldable("tool", parts, { parent: opts && opts.parent }, ts);
  f.body.appendChild(document.createTextNode(toolArgs(args)));
  if (returned) {
    renderToolOutput(f.body, response, {
      responseRef: opts && opts.responseRef,
      responseBytes: opts && opts.responseBytes,
    });
  }
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

// Fold header word for a mid-turn message, by `opts.from`. On the main log's replayed `inbox` item
// this comes from `core/transcripts.py`'s stored provenance tag (`replay_items`), the one honest
// source for "who", since a mixed delivery's text does not reliably carry a marker at all (the
// user's half of it has none) -- which is also why "mixed" is never guessed at anywhere else. On a
// live sub-agent card there is no such tag (see `cardMessageFrom`): that caller passes "agent" only
// when it can read the attribution directly off the text's own leading marker, and nothing in
// between.
const INBOX_LABELS = { agent: "agent", mixed: "mixed" };

// Whether a live sub-agent card's `message` entry opens with an agent's own attribution
// (`core/messaging.py`'s `_for_model`'s `[message from {sender}] `), the one marker that function's
// own docstring calls authentic because only a *leading* one is ever composed there -- anything
// elsewhere in the body could be forged. The card carries no `from` field of its own (principle 1:
// addressing stays out of the channel, so `core/subagents.py` sends only `{"text": ...}`), so this
// is the one place a live view recovers any of what the main log's replayed `inbox` item gets from a
// stored tag, and only the unambiguous half of it: a delivery that joined the user's own words with
// an agent's reads as the plain case here, the same direction `MessageBus.tag_for_delivery` already
// takes when it cannot tell either (showing a worker's note undimmed is a smaller mistake than
// dimming the user's own words).
function cardMessageFrom(text) {
  return /^\[message from [^\]]+\] /.test(text || "") ? "agent" : undefined;
}

// A message sent into a turn already running. Its own foldable class, not `loop`'s: a `loop` row is
// the machine's own event (grouped with thinking/tool/plan as dim, monochrome rows in app.css), and
// the plain case here is a person's own words, which keeps the card's ordinary foreground colour
// rather than reading as something the assistant did.
//
// Three states, not one, since a previous task's reader fix only carried the *user's* mid-turn
// message this far: `opts.from` is absent for the plain case, `"agent"` for a delivery no reader
// should mistake for the user's own, and `"mixed"` for a delivery that joined the two into one
// message (replay only; see `cardMessageFrom` for why a live card never claims this one). An agent's
// note folds into the dimmed `.inbox.agent` treatment below, since it really is a machine event; a
// mixed delivery keeps the undimmed default, since part of it is still the user's own typed words
// and dimming the whole line would misrepresent that part.
function renderMidTurn(text, ts, opts) {
  const from = opts && opts.from;
  const label = Object.prototype.hasOwnProperty.call(INBOX_LABELS, from) ? INBOX_LABELS[from] : "inbox";
  const cls = from === "agent" ? "inbox agent" : from === "mixed" ? "inbox mixed" : "inbox";
  const f = addFoldable(cls, { kind: label }, { parent: opts && opts.parent }, ts);
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
  if (reloadingForNewPage) return;  // the page is being replaced; drawing more would only flash
  frameReceived = true;
  const frame = JSON.parse(event.data);
  if (frame.type === "page") {
    checkPageVersion(frame.version);
    return;
  }
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
  } else if (frame.type === "request") {
    // Arrives when the call completes, so it sits after that round's output; like a tool block it ends
    // the answer segment above it.
    thinkingBlock = null;
    renderRequest(frame);
  } else if (frame.type === "ending") {
    thinkingBlock = null;
    renderEnding(frame);
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
    // Something happened outside the conversation on screen: a background turn finished, a scheduled
    // task reported, an authorization is waiting, a backgrounded turn's gated tool was denied. It goes
    // to the alert layer, so this view and its working indicator are left exactly as they are.
    raiseAlert(frame);
  } else if (frame.type === "working") {
    setProcessing(!!frame.active);
    // `elapsed` is how long the turn has been running server-side, so switching into one that started
    // minutes ago counts on from there rather than restarting at zero.
    setWorking(frame.active ? (frame.elapsed || 0) * 1000 : null);
  } else if (frame.type === "approval") {
    renderApproval(frame.name, frame.arguments, frame.agent);
  } else if (frame.type === "auto_approval") {
    renderAutoApproval(frame);
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
    // A card names its conversation by title and offers a way into it, and a task prunes its older
    // runs, so the list arriving is also the moment a standing card learns its target is gone. Only
    // when a card is actually pointing somewhere: re-rendering the layer re-announces it to a screen
    // reader, and this frame arrives on every turn that saves.
    if (alerts.some((alert) => alert.conversationId)) renderAlerts();
  } else if (frame.type === "tasks") {
    lastTasks = frame.items;
    renderSidebar();
  } else if (frame.type === "models") {
    renderModels(frame);
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
      // The bubble this turn was made of, named by the token that went out with it. A frame with no
      // token, or one naming a bubble this page never drew, is a turn nothing here is waiting on (a
      // proactive run, a scheduled firing) and stamps nothing.
      const pending = claimBubble(frame.token);
      if (pending) {
        // A bubble can arrive here already marked as having joined a running turn: the server
        // accepted it into one, that turn ended before reading it, and it was re-run as a turn of
        // its own. The mark is withdrawn rather than left standing beside the controls, since the
        // two would otherwise say the message both did and did not become a turn.
        pending.classList.remove("mid-turn");
        stampTurnControls(pending, frame.message_index, frame.conversation_id);
        addTurnBadge(pending, frame.turn);
      }
    }
  } else if (frame.type === "inbox") {
    // AIMU's own name for this chunk (renamed from `STEERING`), matched here because both of the
    // server's emitters now send it: a plain reactive turn's delivery frame and a planned turn's
    // both carry this type, and a mid-turn message's live marker would render nothing without this
    // branch, on either kind of turn.
    //
    // The bubble's other possible fate, and the reason this map matches rather than counts: the
    // message joined the turn already running, so no `turn_saved` for a turn of its own will name it.
    // Marked rather than stamped, and deliberately given no turn controls: while that holds it has no
    // turn, and the index that would branch or truncate "here" points into the middle of somebody
    // else's turn.
    //
    // Looked up without claiming, which is the one place this map is read that way: an acceptance is
    // not the message's final fate. A turn that ends before reading an accepted message hands it back
    // and it runs as a turn after all, carrying this same token on that turn's save, so dropping the
    // entry here would leave the bubble that did become a turn with no way to be named. The entry
    // left behind when the message *is* delivered is inert, for the reason the map's own comment
    // gives: only a frame carrying this token can ever claim it, and nothing else mints one.
    //
    // Two frames of this type arrive for one mid-turn message and only one carries a token. This is
    // the server reporting the message accepted, and the token is what names the bubble; the other
    // comes from the agent loop as it reads the message, which is the one a sub-agent card renders
    // and which names no bubble. So an untokened frame marks nothing here, as a `turn_saved` with
    // no token stamps nothing just above.
    const midTurn = pendingBubbles.get(frame.token);
    if (midTurn) midTurn.classList.add("mid-turn");
  } else if (frame.type === "history") {
    // Replay a conversation (on connect or after switching), reusing the live renderers.
    closeRoundPanel();  // it points into the log being replaced
    topRounds = [];
    latestRound.delete(log);  // its button lived in the transcript being replaced
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
    pendingBubbles = new Map();  // the bubbles these referred to were just detached by the repaint
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
      if (item.type === "user") {
        const bubble = addBubble("user", item.text, item.ts);
        addTurnBadge(bubble, item.turn);
        stampTurnStart(bubble);
      }
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
        if (item.from !== "assistant") {
          addTurnBadge(bubble, item.turn);
          stampTurnStart(bubble);
        }
      }
      else if (item.type === "request") renderRequest(item);
      else if (item.type === "ending") renderEnding(item);
      else if (item.type === "thinking") {
        const f = addFoldable("thinking", { kind: "thinking" }, undefined, item.ts);
        f.body.textContent = item.text || "";
        setFoldLabel(f.label, "thinking", "", lineMetric(item.text));
      }
      else if (item.type === "tool") renderTool(item.name, item.arguments, item.ts, { response: item.response });
      else if (item.type === "loop") renderLoop(item.text, item.ts, { reason: item.reason });
      // A message sent into a turn already running, read from the stored record
      // (`record_turn_provenance`'s `messages` map) rather than a frame about a turn still open: this
      // item type reaches here for a turn long since finished and saved exactly as it does for one
      // still in flight when the page connected, since the item carries nothing saying which. Rendered
      // as a collapsed row rather than the live view's marked user bubble (the `.mid-turn` class), a
      // deliberate divergence reasoned about in `replay_items`'s own docstring rather than here.
      // `item.from` is absent for the user's own words and `"agent"` / `"mixed"` for the two cases
      // `replay_items` tells apart from the stored provenance tag; see `renderMidTurn` for why those
      // three states render differently.
      else if (item.type === "inbox") renderMidTurn(item.text, item.ts, { from: item.from });
      else if (item.type === "subagent") renderSubagent(item, item.ts);
      else if (item.type === "phase") renderPhase(item.label, item.detail, item.ts);
      else if (item.type === "reasoning") addMarkdownBubble("assistant", item.text, item.ts);
      else if (item.type === "message" && !isBlank(item.text)) addMarkdownBubble(item.proactive ? "proactive" : "assistant", item.text, item.ts);
      // Why a turn stopped early. A scheduled run's error never reached this conversation live, so on
      // reload this is the only account of it the conversation has.
      else if (item.type === "notice") addBubble("notice", item.text, item.ts);
      // One of the turn's own agents sent this and no reader ever took it (`MessageBus.close()`'s
      // report, see `core/transcripts.py`'s `undelivered` item). The live turn said so too
      // (`TurnRunner._report_undeliverable`), as an ordinary send rather than a frame of its own, so
      // without this branch a reload lost that account entirely. "Undelivered:" up front is the fact
      // that makes this row worth keeping at all -- without it the sender and the selector alone read
      // as a message that arrived, which is the opposite of what happened -- and the sender and the
      // selector after it are what a reader can act on, the same two facts the live sentence and the
      // Markdown export both lead with.
      else if (item.type === "undelivered") addBubble("notice", `Undelivered: ${item.sender} -> ${item.to}: ${item.text}`, item.ts);
    }
    autoscroll();
  } else if (frame.type === "ready") {
    // The connect sequence is complete: the assistant has connected its remote servers and built its
    // agent, so the sidebar's rows can act. Also the marker that this connection got that far, which
    // is what tells a later close apart from a startup that failed (see connect()).
    document.body.classList.remove("booting");
    synced = true;
  }
}

// Which model is answering, in the header. Painted once per connection and never updated, because the
// server cannot tell it anything newer: a model is read as the agents are built and is not a runtime
// setting, so it holds for the life of the process that sent this frame. The label is the entry agent's
// model and the tooltip is the whole record, each agent that declares its own on a line of its own,
// which is what `/diag` prints as prose in the terminal.
//
// textContent and `title` throughout, never markup: the strings are config.toml's, and `update_config`
// is a tool the assistant holds, so they are no more trusted here than a tool result is.
function renderModels(frame) {
  const model = typeof frame.model === "string" ? frame.model : "";
  modelLabel.textContent = model;
  const lines = model ? [`${frame.agent}: ${model}`] : [];
  const overrides = frame.overrides && typeof frame.overrides === "object" ? frame.overrides : {};
  for (const [agent, declared] of Object.entries(overrides)) {
    // The entry agent appears in both halves when its own table names a model, and the label above is
    // already that string; the record stays faithful to what the config declares, so the skip is here.
    if (agent !== frame.agent) lines.push(`${agent}: ${declared}`);
  }
  modelLabel.title = lines.join("\n");
}

// A gated tool needs confirmation: show the call and Allow/Deny buttons. The reply is a plain
// "y"/"n" frame, which the server routes to the waiting tool call (same path as typing it).
function renderApproval(name, args, agent) {
  const el = document.createElement("div");
  el.className = "bubble approval";
  const prompt = document.createElement("div");
  prompt.className = "prompt";
  // Naming the agent matters because a delegated worker's call lands in the same log as the entry
  // agent's; without it the user cannot tell whose action they are allowing.
  prompt.textContent = agent ? `Allow ${agent} to run ` : "Allow ";
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

// A reviewer answered for a gated tool call instead of the user, so this reports the decision rather
// than asking one: nothing replies to it. It shares the approval prompt's warm styling with a modifier
// class, so an auto-approval reads as the same kind of decision, just reached without the user. An
// escalation reads as the preface to the ordinary approval prompt arriving right behind it, not as a
// refusal, which is why it says the call is coming to the user rather than that it was turned down.
function renderAutoApproval(frame) {
  // The decision belongs to one turn, so a card that raced a conversation switch is dropped rather
  // than drawn into the transcript now on screen. The server already re-checks this after the review
  // and sends nothing for a turn you switched away from; this covers the gap between that check and
  // the frame arriving. Same shape as `turn_saved` above.
  const active = lastConversations.find((item) => item.active);
  if (frame.conversation_id && active && active.id !== frame.conversation_id) return;
  const el = document.createElement("div");
  el.className = frame.approved ? "bubble approval auto-approved" : "bubble approval auto-escalated";
  const prompt = document.createElement("div");
  prompt.className = "prompt";
  // The agent is named for the reason the approval prompt names it: a worker's call lands in the same log.
  const call = frame.agent ? `${frame.agent}'s ` : "";
  prompt.textContent = frame.approved
    ? `${frame.model} auto-approved ${call}`
    : `${frame.model} is asking you to approve ${call}`;
  const code = document.createElement("code");
  code.textContent = toolLine(frame.name, frame.arguments);
  prompt.appendChild(code);
  // Only when there is a sentence: this is what a user reads about a call that was waved through, and
  // a reviewer that answered with an empty reason should not leave it trailing a bare colon.
  if (frame.reason) prompt.appendChild(document.createTextNode(`: ${frame.reason}`));
  el.appendChild(prompt);
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
// the one-connection guard's "busy in another tab" (or "still closing", when the connection a reload
// replaced outlasted the server's wait for its teardown), or a config error reported in place of a
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
  // Every connection starts here, including a reconnect: the new server has its own assistant to
  // start, so the page is un-ready again until that connection's `ready` frame lands.
  document.body.classList.add("booting");
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
    if (reloadingForNewPage) return;  // the reload closes this socket; it is not an outage
    // A closed socket is not a booting one, on any path that reaches here: the one-connection
    // refusal below, a create()/start() failure the server closed on, or an ordinary outage.
    // Leaving the class set would stack "Starting up..." above "Disconnected...", and freeze the
    // sidebar's pointer-events: none with nothing left to lift it once no ready frame is coming.
    document.body.classList.remove("booting");
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

// The version of the page this tab was served, written into index.html by the server. Reconnecting
// resyncs the data but not the script, so a restart that changed the page would leave this tab drawing
// the new server's frames with the old code. Every connection opens with a `page` frame naming the
// version the server serves now, and a mismatch reloads the tab (see frontends/web.py's
// _page_version). The literal placeholder means the page was not served through that substitution,
// so there is nothing to compare.
const PAGE_VERSION_META = document.querySelector('meta[name="kokua-page-version"]');
const PAGE_VERSION = PAGE_VERSION_META && !PAGE_VERSION_META.content.startsWith("__") ? PAGE_VERSION_META.content : "";
// Session-scoped, so they live exactly as long as the tab: what the composer held when it reloaded,
// and which version it reloaded for.
const DRAFT_KEY = "kokua-draft";
const RELOADED_FOR_KEY = "kokua-reloaded-for";
let reloadingForNewPage = false;

function checkPageVersion(version) {
  if (!PAGE_VERSION || !version || version === PAGE_VERSION) return;
  // Reloading for the same version twice means the reload did not bring that version in (the page
  // and the socket disagree for some other reason), and a third would be a loop. Stay on this page,
  // which still works, rather than flicker forever.
  try {
    if (sessionStorage.getItem(RELOADED_FOR_KEY) === version) return;
    sessionStorage.setItem(RELOADED_FOR_KEY, version);
    if (input.value) sessionStorage.setItem(DRAFT_KEY, input.value);
  } catch (e) {
    return;  // storage refused: without the loop guard a reload is not safe to attempt
  }
  reloadingForNewPage = true;
  location.reload();
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

// Put back what the composer held when checkPageVersion reloaded the tab, once.
try {
  const draft = sessionStorage.getItem(DRAFT_KEY);
  if (draft) {
    input.value = draft;
    autoGrowInput();
  }
  sessionStorage.removeItem(DRAFT_KEY);
} catch (e) {}

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
  // Sent while a turn is running, so it is a mid-turn message even before the server says so: round
  // buttons look past it to the output above (see ROUND_PASSTHROUGH).
  const sentMidTurn = !stopBtn.classList.contains("hidden");
  if (opening && sentMidTurn) opening.classList.add("sent-mid-turn");
  for (const item of attached) {
    const bubble = addImageBubble(item.dataUrl, "user", now);  // echo attachments locally
    if (!opening) opening = bubble;
  }
  const thinking = thinkingChoice();
  // What the server will actually parse, which is not always what was typed. A message carrying
  // anything besides its text never takes the /plan wrapper: an image turn has never been plannable,
  // and the picker is disabled whenever Plan is on, so extras and a wrapper can never both be wanted.
  // `thinking` is tested for truthiness here, the same way the frame body below decides whether to
  // set `frame.thinking`: a future reasoning-effort option with a falsy non-null value must not go as
  // a frame with no `thinking` key while also silently keeping the /plan wrapper this line exists to
  // withhold from it.
  const carriesExtras = attached.length > 0 || !!thinking;
  const outgoing = !carriesExtras && planNext && !/^\/plan(\s|$)/i.test(text) ? "/plan " + text : text;
  // The name this page and the server will both use for the bubble just drawn, until the server says
  // what became of it. Minted for every message, including text the server answers as a command and
  // runs no turn for: that entry is simply never claimed (see `pendingBubbles`), so nothing has to be
  // predicted here about how the server will read the words.
  const token = `${bubbleEpoch}-${++bubbleTokens}`;
  // The conversation is consulted and not recorded: every repaint clears this map (the bubbles in it
  // are detached by the same repaint), so an entry can only ever belong to the conversation in view,
  // and a bubble drawn before the page knows which conversation that is could never be stamped anyway.
  if (opening && lastConversations.some((item) => item.active)) pendingBubbles.set(token, opening);
  // Always a frame, never bare text: the token has nowhere to ride on a plain string, and the
  // alternative (a frame only for a message carrying extras) would leave the commonest message of all
  // unable to be matched to its turn. The server reads a frame's text exactly as it reads a bare one
  // (`_parse_input` feeds it straight through, commands and all), so this changes what the message
  // travels in and nothing about what it says. The approval and /stop replies the page sends from its
  // own controls are still bare text: those answer a turn rather than opening one, and draw no bubble.
  const frame = { type: "input", text: outgoing, token };
  if (attached.length) frame.images = attached.map(a => a.dataUrl);
  if (thinking) frame.thinking = thinking;
  ws.send(JSON.stringify(frame));
  if (attached.length) {
    attached = [];
    renderPreviews();
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
