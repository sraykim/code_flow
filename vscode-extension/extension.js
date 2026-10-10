// Code Flow: see a Python script as a dataflow graph of draggable code blocks.
"use strict";
const vscode = require("vscode");
const cp = require("child_process");
const path = require("path");
const fs = require("fs");
const os = require("os");
const crypto = require("crypto");

/** fsPath -> { panel, file, graph, busy } */
const panels = new Map();
let pythonCmd = null; // cached working interpreter: { cmd, args }

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand("codeFlow.open", (uri) => openFlow(context, uri)),
    vscode.commands.registerCommand("codeFlow.explain", async (uri) => {
      const file = resolveFile(uri);
      if (!file) return;
      await openFlow(context, vscode.Uri.file(file));
      explain(context, file);
    }),
    vscode.commands.registerCommand("codeFlow.export", async (uri) => {
      const file = resolveFile(uri);
      if (!file) return;
      await openFlow(context, vscode.Uri.file(file));
      post(panels.get(file), { type: "exportRequest" });
    }),
    vscode.commands.registerCommand("codeFlow.chat", async (uri) => {
      const file = resolveFile(uri);
      if (!file) return;
      await openFlow(context, vscode.Uri.file(file));
      post(panels.get(file), { type: "chatOpen" });
    }),
    vscode.workspace.onDidSaveTextDocument((doc) => {
      if (panels.has(doc.uri.fsPath)) refresh(context, doc.uri.fsPath);
    }),
    vscode.debug.registerDebugAdapterTrackerFactory("*", { createDebugAdapterTracker: (session) => debugTracker(session) }),
    vscode.debug.onDidTerminateDebugSession(() => { for (const st of panels.values()) if (st.debugging) { st.debugging = false; post(st, { type: "debug", event: "ended" }); } })
  );
}

function deactivate() {}

// ------------------------------------------------------------------ panel
function resolveFile(uri) {
  const u = uri instanceof vscode.Uri ? uri : vscode.window.activeTextEditor && vscode.window.activeTextEditor.document.uri;
  if (!u || u.scheme !== "file" || path.extname(u.fsPath).toLowerCase() !== ".py") {
    vscode.window.showWarningMessage("Code Flow: open or select a Python (.py) file first.");
    return null;
  }
  return u.fsPath;
}

async function openFlow(context, uri) {
  const file = resolveFile(uri);
  if (!file) return;
  const existing = panels.get(file);
  if (existing) {
    existing.panel.reveal(undefined, true);
    await refresh(context, file);
    return;
  }
  const panel = vscode.window.createWebviewPanel(
    "codeFlow",
    "Flow · " + path.basename(file),
    { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
    { enableScripts: true, retainContextWhenHidden: true, localResourceRoots: [] }
  );
  panel.iconPath = new vscode.ThemeIcon("type-hierarchy");
  const st = { panel, file, graph: null, busy: false, chat: null, debugging: false, traceBps: [] };
  panels.set(file, st);
  panel.onDidDispose(() => { clearTrace(st); if (st.chat && st.chat.child) st.chat.child.kill(); panels.delete(file); });
  panel.webview.onDidReceiveMessage((m) => onMessage(context, st, m));

  let graph;
  try {
    graph = await runParser(context, file);
  } catch (e) {
    graph = { error: String((e && e.message) || e) };
  }
  st.graph = graph;
  const enrichment = savedEnrichment(context, st);
  const positions = context.workspaceState.get("pos:" + file) || null;
  panel.webview.html = buildHtml(context, graph, enrichment, positions);
  if (graph.error) vscode.window.showWarningMessage("Code Flow: " + graph.error);
  else if (!enrichment && vscode.workspace.getConfiguration("codeFlow").get("autoExplain")) explain(context, file);
}

async function refresh(context, file) {
  const st = panels.get(file);
  if (!st) return;
  let graph;
  try {
    graph = await runParser(context, file);
  } catch (e) {
    graph = { error: String((e && e.message) || e) };
  }
  if (graph.error) {
    post(st, { type: "status", text: graph.error + (graph.line ? ` (line ${graph.line})` : ""), error: true });
    return;
  }
  st.graph = graph;
  const enrichment = savedEnrichment(context, st);
  post(st, { type: "graph", graph, enrichment });
  if (!enrichment && context.workspaceState.get("enrich:" + file)) {
    post(st, { type: "status", text: "Code changed. Click ✦ Explain for fresh notes." });
  }
}

function buildHtml(context, graph, enrichment, positions) {
  const nonce = crypto.randomBytes(16).toString("hex");
  const csp = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}'; img-src data:;">`;
  let html = fs.readFileSync(path.join(context.extensionPath, "media", "viewer.html"), "utf8");
  html = html
    .replace("<!--__CF_CSP__-->", csp)
    .replace("/*__CODEFLOW_GRAPH__*/null", () => toJs(graph))
    .replace("/*__CODEFLOW_ENRICH__*/null", () => (enrichment ? toJs(enrichment) : "null"))
    .replace("/*__CODEFLOW_POSITIONS__*/null", () => (positions ? toJs(positions) : "null"))
    .replace("/*__CODEFLOW_OPTIONS__*/null", () => toJs({
      direction: vscode.workspace.getConfiguration("codeFlow").get("direction") === "vertical" ? "TB" : "LR",
    }))
    .split("__CF_NONCE__").join(nonce);
  return html;
}

function toJs(obj) {
  return JSON.stringify(obj)
    .replace(/<\//g, "<\\/")
    .replace(/<!--/g, "<\\!--")
    .replace(/\u2028/g, "\\u2028")
    .replace(/\u2029/g, "\\u2029");
}

function post(st, msg) {
  try {
    st.panel.webview.postMessage(msg);
  } catch (_) { /* panel closed */ }
}

async function onMessage(context, st, m) {
  if (!m || typeof m !== "object") return;
  if (m.type === "reveal") reveal(st.file, m.line, m.end);
  else if (m.type === "positions") context.workspaceState.update("pos:" + st.file, m.positions);
  else if (m.type === "explain") explain(context, st.file);
  else if (m.type === "chat") chat(context, st, m);
  else if (m.type === "chatStop") { if (st.chat && st.chat.child) st.chat.child.kill(); }
  else if (m.type === "chatReset") { if (st.chat && st.chat.child) st.chat.child.kill(); st.chat = null; }
  else if (m.type === "trace") setTrace(st, !!m.on, m.lines || []);
  else if (m.type === "export") exportHtml(context, st, m.positions);
}

// ------------------------------------------------------------------ export
async function exportHtml(context, st, positions) {
  if (!st.graph || st.graph.error) { vscode.window.showWarningMessage("Code Flow: nothing to export until the script parses."); return; }
  const base = path.basename(st.file, ".py");
  const target = await vscode.window.showSaveDialog({
    defaultUri: vscode.Uri.file(path.join(path.dirname(st.file), base + ".flow.html")),
    filters: { "HTML page": ["html"] },
    title: "Export Code Flow map",
  });
  if (!target) return;
  const nonce = crypto.randomBytes(16).toString("hex");
  let html = fs.readFileSync(path.join(context.extensionPath, "media", "viewer.html"), "utf8");
  html = html
    .replace("<!--__CF_CSP__-->", "")
    .replace("/*__CODEFLOW_GRAPH__*/null", () => toJs(st.graph))
    .replace("/*__CODEFLOW_ENRICH__*/null", () => { const e = savedEnrichment(context, st); return e ? toJs(e) : "null"; })
    .replace("/*__CODEFLOW_POSITIONS__*/null", () => (positions ? toJs(positions) : "null"))
    .replace("/*__CODEFLOW_OPTIONS__*/null", () => toJs({ direction: positions && positions.dir === "TB" ? "TB" : "LR" }))
    .split("__CF_NONCE__").join(nonce);
  fs.writeFileSync(target.fsPath, html, "utf8");
  const open = await vscode.window.showInformationMessage(`Code Flow: exported ${path.basename(target.fsPath)}`, "Open in browser", "Reveal in Finder/Explorer");
  if (open === "Open in browser") vscode.env.openExternal(target);
  else if (open) vscode.commands.executeCommand("revealFileInOS", target);
}

async function reveal(file, line, end) {
  const uri = vscode.Uri.file(file);
  const visible = vscode.window.visibleTextEditors.find((e) => e.document.uri.fsPath === file);
  const doc = await vscode.workspace.openTextDocument(uri);
  const editor = await vscode.window.showTextDocument(doc, {
    viewColumn: visible ? visible.viewColumn : vscode.ViewColumn.One,
    preserveFocus: false,
  });
  const start = Math.max(0, (line || 1) - 1);
  const stop = Math.max(start, (end || line || 1) - 1);
  const range = new vscode.Range(start, 0, stop, doc.lineAt(Math.min(stop, doc.lineCount - 1)).text.length);
  editor.selection = new vscode.Selection(range.start, range.end);
  editor.revealRange(range, vscode.TextEditorRevealType.InCenterIfOutsideViewport);
}

// ------------------------------------------------------------------ parser (Python)
function execFileP(cmd, args, opts) {
  return new Promise((resolve, reject) => {
    cp.execFile(cmd, args, { maxBuffer: 64 * 1024 * 1024, timeout: 60000, ...opts }, (err, stdout, stderr) => {
      if (err) {
        err.stdout = stdout;
        err.stderr = stderr;
        reject(err);
      } else resolve(stdout);
    });
  });
}

function pythonCandidates() {
  const configured = vscode.workspace.getConfiguration("codeFlow").get("pythonPath");
  const list = [];
  if (configured) list.push({ cmd: configured, args: [] });
  if (process.platform === "win32") list.push({ cmd: "py", args: ["-3"] }, { cmd: "python", args: [] });
  else list.push({ cmd: "python3", args: [] }, { cmd: "python", args: [] }, { cmd: "/usr/bin/python3", args: [] });
  return list;
}

async function runParser(context, file, extra = []) {
  const script = path.join(context.extensionPath, "python", "codeflow_parse.py");
  const candidates = pythonCmd ? [pythonCmd, ...pythonCandidates()] : pythonCandidates();
  let lastErr = null;
  for (const py of candidates) {
    try {
      const out = await execFileP(py.cmd, [...py.args, script, file, ...extra], { cwd: path.dirname(file) });
      pythonCmd = py;
      return extra.includes("--brief") ? out : JSON.parse(out);
    } catch (e) {
      if (e.code === "ENOENT") continue; // interpreter not found, try the next one
      // A syntax error in the user's script comes back as JSON on stdout.
      if (e.stdout) {
        try {
          pythonCmd = py;
          return JSON.parse(e.stdout);
        } catch (_) { /* fall through */ }
      }
      lastErr = new Error((e.stderr || e.message || String(e)).trim().split("\n").slice(-1)[0]);
      if (/SyntaxError|invalid syntax/.test(String(e.stderr))) break;
    }
  }
  throw lastErr || new Error("Python 3 was not found. Set codeFlow.pythonPath in Settings.");
}

// ------------------------------------------------------------------ Claude Code
function savedEnrichment(context, st) {
  const e = context.workspaceState.get("enrich:" + st.file);
  return e && st.graph && e.source_hash === st.graph.source_hash ? e : null;
}

function claudeCandidates() {
  const configured = vscode.workspace.getConfiguration("codeFlow").get("claudePath");
  const home = os.homedir();
  const list = [configured || "claude"];
  // GUI-launched editors often miss shell PATH entries; try the usual install spots.
  for (const p of [
    path.join(home, ".claude", "local", "claude"),
    path.join(home, ".local", "bin", "claude"),
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
  ]) {
    if (fs.existsSync(p) && !list.includes(p)) list.push(p);
  }
  return list;
}

function runClaude(bin, prompt, cwd, token, extraArgs = [], onChild = null) {
  const cfg = vscode.workspace.getConfiguration("codeFlow");
  const args = ["-p", "--output-format", "json", ...extraArgs];
  const model = cfg.get("claudeModel");
  if (model) args.push("--model", model);
  return new Promise((resolve, reject) => {
    const child = cp.spawn(bin, args, { cwd, shell: process.platform === "win32", env: process.env });
    if (onChild) onChild(child);
    let out = "", err = "", done = false;
    const finish = (fn, v) => { if (!done) { done = true; clearTimeout(timer); fn(v); } };
    const timer = setTimeout(() => {
      child.kill();
      finish(reject, new Error(`Claude Code did not answer within ${cfg.get("timeoutSeconds")} s`));
    }, (cfg.get("timeoutSeconds") || 240) * 1000);
    if (token) token.onCancellationRequested(() => { child.kill(); finish(reject, new Error("Cancelled")); });
    child.stdout.on("data", (d) => (out += d));
    child.stderr.on("data", (d) => (err += d));
    child.on("error", (e) => finish(reject, e));
    child.on("close", (code) => {
      if (code !== 0 && !out.trim()) finish(reject, new Error(err.trim() || `claude exited with code ${code}`));
      else finish(resolve, out);
    });
    child.stdin.on("error", () => {});
    child.stdin.end(prompt);
  });
}

function extractJson(text) {
  let t = String(text || "").trim();
  const fence = t.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (fence) t = fence[1];
  const a = t.indexOf("{"), b = t.lastIndexOf("}");
  if (a < 0 || b <= a) throw new Error("Claude's answer did not contain JSON");
  return JSON.parse(t.slice(a, b + 1));
}

function cleanEnrichment(raw, graph) {
  const ids = new Set(graph.nodes.map((n) => n.id));
  for (const [fname, f] of Object.entries(graph.flows || {})) for (const n of f.nodes) ids.add(fname + "/" + n.id);
  const secIds = new Set((graph.sections || []).map((x) => x.id));
  const strList = (x) => (Array.isArray(x) ? x.filter((s) => typeof s === "string" && s.trim()).map((s) => s.trim()).slice(0, 20) : []);
  const out = { source_hash: graph.source_hash, summary: typeof raw.summary === "string" ? raw.summary : "",
    inputs: strList(raw.inputs), outputs: strList(raw.outputs), sections: {}, nodes: {}, stages: [], functions: {}, implicit_edges: [] };
  for (const [id, v] of Object.entries(raw.sections || {})) {
    if (secIds.has(id) && v && typeof v.summary === "string") {
      out.sections[id] = { summary: v.summary };
      if (typeof v.narrative === "string" && v.narrative.trim()) out.sections[id].narrative = v.narrative.trim();
    }
  }
  for (const [name, v] of Object.entries(raw.functions || {})) {
    if (graph.flows && graph.flows[name] && v && typeof v.narrative === "string" && v.narrative.trim()) out.functions[name] = { narrative: v.narrative.trim() };
  }
  for (const [id, v] of Object.entries(raw.nodes || {})) {
    if (ids.has(id) && v && typeof v.summary === "string") {
      out.nodes[id] = { summary: v.summary };
      if (["key", "support", "minor"].includes(v.role)) out.nodes[id].role = v.role;
      if (typeof v.watch === "string" && v.watch.trim()) out.nodes[id].watch = v.watch.trim();
    }
  }
  for (const s of Array.isArray(raw.stages) ? raw.stages : []) {
    const nodes = (s.nodes || []).filter((id) => ids.has(id));
    if (s.name && nodes.length) out.stages.push({ name: String(s.name), summary: typeof s.summary === "string" ? s.summary : "",
      narrative: typeof s.narrative === "string" ? s.narrative.trim() : "", nodes });
  }
  for (const e of Array.isArray(raw.implicit_edges) ? raw.implicit_edges : []) {
    if (ids.has(e.source) && ids.has(e.target) && e.source !== e.target)
      out.implicit_edges.push({ source: e.source, target: e.target, label: String(e.label || "implicit"), reason: String(e.reason || "") });
  }
  return out;
}

async function explain(context, file) {
  const st = panels.get(file);
  if (!st || !st.graph || st.graph.error || st.busy) return;
  st.busy = true;
  post(st, { type: "status", text: "Claude Code is reading the script…", busy: true });
  try {
    const brief = await runParser(context, file, ["--brief"]);
    const source = fs.readFileSync(file, "utf8").split(/\r?\n/).map((l, i) => `${String(i + 1).padStart(4)}  ${l}`).join("\n");
    const template = fs.readFileSync(path.join(context.extensionPath, "media", "explain_prompt.md"), "utf8");
    const prompt = `${template}\n\n${brief}\nSCRIPT (line numbers on the left):\n${source}\n`;

    const raw = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Code Flow: Claude Code is reading ${path.basename(file)}`, cancellable: true },
      async (_p, token) => {
        let lastErr;
        for (const bin of claudeCandidates()) {
          try {
            return await runClaude(bin, prompt, path.dirname(file), token);
          } catch (e) {
            lastErr = e;
            if (e.code !== "ENOENT") throw e;
          }
        }
        throw lastErr;
      }
    );
    let text = raw;
    try {
      const obj = JSON.parse(raw);
      if (obj.is_error) throw new Error(obj.result || obj.subtype || "Claude Code returned an error");
      text = obj.result != null ? obj.result : raw;
    } catch (e) {
      if (e instanceof SyntaxError) text = raw; else throw e;
    }
    const enrichment = cleanEnrichment(extractJson(text), st.graph);
    await context.workspaceState.update("enrich:" + file, enrichment);
    post(st, { type: "enrichment", enrichment });
    post(st, { type: "status", text: "" });
  } catch (e) {
    const missing = e && e.code === "ENOENT";
    const msg = missing
      ? "Claude Code CLI not found. Install Claude Code and run `claude` once to sign in, or set codeFlow.claudePath. The parser's map still works without it."
      : `Claude Code could not explain this script: ${(e && e.message) || e}`;
    post(st, { type: "status", text: missing ? "Claude Code not found: showing the parser's map only" : "Explain failed: see notification", error: true });
    if (!(e && e.message === "Cancelled")) vscode.window.showWarningMessage("Code Flow: " + msg);
  } finally {
    st.busy = false;
    post(st, { type: "status", busy: false, text: undefined });
  }
}

// ------------------------------------------------------------------ chat
function chatSystemPrompt() {
  return [
    "You are answering questions inside Code Flow, a VS Code panel that shows a Python script as a dataflow map of blocks",
    "(each top-level statement or function call is a block; arrows carry the variables passed between them; functions can be opened",
    "to show their own inner blocks). The user is looking at that map while they chat with you.",
    "",
    "Rules:",
    "- Answer from the script below. Be concrete: name variables, columns, thresholds, defaults and the lines that matter.",
    "- Refer to blocks by their ids in backticks (for example `n5` or `_attribute_wismo/n3`); the panel turns those into links that",
    "  highlight the block on the map. Refer to code locations as `L123` or `L120-130`; those become links into the editor.",
    "- Each user message starts with a [context] line that says what is selected or open on the map and where the debugger is paused,",
    "  if it is running. 'The selected block', 'this', 'here' refer to that context.",
    "- Keep answers short and skimmable: a few sentences, or a short list. No preamble. Only use code fences for code.",
    "- You may read other files in the workspace with your tools if the script imports them, but do not modify anything.",
    "- The first user message carries the block list (BLOCKS/EDGES/FLOWS) and the full script; later messages only carry the question.",
  ].join("\n");
}

function describeState(state) {
  if (!state) return "[context] nothing selected";
  const bits = [];
  if (state.selected) bits.push(`selected block ${state.selected.id}${state.selected.title ? ` "${state.selected.title}"` : ""}${state.selected.lines ? ` (L${state.selected.lines[0]}-${state.selected.lines[1]})` : ""}`);
  if (state.open_functions && state.open_functions.length) bits.push(`open functions: ${state.open_functions.join(", ")}`);
  if (state.open_sections && state.open_sections.length) bits.push(`open sections: ${state.open_sections.join(", ")}`);
  if (state.debugger) bits.push(`debugger paused at L${state.debugger.stopped_at_line} in block ${state.debugger.block}; visited so far: ${(state.debugger.visited || []).join(" → ")}`);
  return "[context] " + (bits.join("; ") || "nothing selected");
}

async function chat(context, st, m) {
  if (!st.graph || st.graph.error) { post(st, { type: "chatReply", error: "the script could not be parsed" }); return; }
  if (st.chat && st.chat.child) { post(st, { type: "chatReply", error: "still answering the previous question" }); return; }
  try {
    if (!st.chat || st.chat.hash !== st.graph.source_hash) {
      // New conversation (or the script changed): rebuild the context once; the session id keeps the follow-ups cheap.
      const brief = await runParser(context, st.file, ["--brief"]);
      const source = fs.readFileSync(st.file, "utf8").split(/\r?\n/).map((l, i) => `${String(i + 1).padStart(4)}  ${l}`).join("\n");
      st.chat = { id: crypto.randomUUID(), started: false, hash: st.graph.source_hash, system: chatSystemPrompt(),
                  intro: `${brief}\nSCRIPT (line numbers on the left):\n${source}\n`, child: null };
    }
    const c = st.chat;
    const args = ["--append-system-prompt", c.system, "--allowedTools", "Read", "Grep", "Glob", c.started ? "--resume" : "--session-id", c.id];
    const prompt = (c.started ? "" : c.intro + "\n") + `${describeState(m.state)}\n${m.text}`;
    let raw, lastErr;
    for (const bin of claudeCandidates()) {
      try {
        raw = await runClaude(bin, prompt, path.dirname(st.file), null, args, (child) => { c.child = child; });
        break;
      } catch (e) {
        lastErr = e;
        if (e.code !== "ENOENT") throw e;
      }
    }
    if (raw == null) throw lastErr;
    c.started = true;
    let text = raw;
    try {
      const obj = JSON.parse(raw);
      if (obj.is_error) throw new Error(obj.result || obj.subtype || "Claude Code returned an error");
      text = obj.result != null ? obj.result : raw;
      if (obj.session_id) c.id = obj.session_id;
    } catch (e) {
      if (!(e instanceof SyntaxError)) throw e;
    }
    post(st, { type: "chatReply", text: String(text).trim() });
  } catch (e) {
    const missing = e && e.code === "ENOENT";
    post(st, { type: "chatReply", error: missing ? "Claude Code CLI not found. Install Claude Code and run `claude` once to sign in, or set codeFlow.claudePath." : String((e && e.message) || e) });
  } finally {
    if (st.chat) st.chat.child = null;
  }
}

// ------------------------------------------------------------------ debugger follow
const samePath = (a, b) => a && b && (process.platform === "win32" || process.platform === "darwin" ? a.toLowerCase() === b.toLowerCase() : a === b);

function debugTracker(session) {
  return {
    onDidSendMessage: async (msg) => {
      if (!msg || msg.type !== "event") return;
      if (msg.event === "stopped") {
        let frames = [];
        try {
          const r = await session.customRequest("stackTrace", { threadId: msg.body.threadId, startFrame: 0, levels: 40 });
          frames = (r && r.stackFrames) || [];
        } catch (_) { return; }
        for (const st of panels.values()) {
          const mine = frames.filter((f) => f.source && samePath(f.source.path, st.file)).map((f) => ({ line: f.line }));
          if (!mine.length) continue;
          if (!st.debugging) { st.debugging = true; post(st, { type: "debug", event: "start" }); }
          post(st, { type: "debug", event: "stopped", frames: mine });
        }
      } else if (msg.event === "terminated" || msg.event === "exited") {
        for (const st of panels.values()) if (st.debugging) { st.debugging = false; post(st, { type: "debug", event: "ended" }); }
      }
    },
  };
}

function setTrace(st, on, lines) {
  clearTrace(st);
  if (!on) return;
  const uri = vscode.Uri.file(st.file);
  const have = new Set(vscode.debug.breakpoints.filter((b) => b instanceof vscode.SourceBreakpoint && samePath(b.location.uri.fsPath, st.file)).map((b) => b.location.range.start.line));
  st.traceBps = lines.filter((l) => !have.has(l - 1)).map((l) => new vscode.SourceBreakpoint(new vscode.Location(uri, new vscode.Position(l - 1, 0)), true, undefined, undefined, "Code Flow trace"));
  if (st.traceBps.length) vscode.debug.addBreakpoints(st.traceBps);
  post(st, { type: "status", text: `trace: ${lines.length} block breakpoints set — start the debugger (F5) and press Continue to walk the map` });
}
function clearTrace(st) {
  if (st.traceBps && st.traceBps.length) { try { vscode.debug.removeBreakpoints(st.traceBps); } catch (_) {} }
  st.traceBps = [];
}

module.exports = { activate, deactivate, _test: { extractJson, cleanEnrichment, toJs, describeState } };
