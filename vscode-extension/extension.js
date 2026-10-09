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
    vscode.workspace.onDidSaveTextDocument((doc) => {
      if (panels.has(doc.uri.fsPath)) refresh(context, doc.uri.fsPath);
    })
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
  const st = { panel, file, graph: null, busy: false };
  panels.set(file, st);
  panel.onDidDispose(() => panels.delete(file));
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

function runClaude(bin, prompt, cwd, token) {
  const cfg = vscode.workspace.getConfiguration("codeFlow");
  const args = ["-p", "--output-format", "json"];
  const model = cfg.get("claudeModel");
  if (model) args.push("--model", model);
  return new Promise((resolve, reject) => {
    const child = cp.spawn(bin, args, { cwd, shell: process.platform === "win32", env: process.env });
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

module.exports = { activate, deactivate, _test: { extractJson, cleanEnrichment, toJs } };
