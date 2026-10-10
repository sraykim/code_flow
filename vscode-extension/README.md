# Code Flow

See a Python script as a dataflow map instead of top-to-bottom text.

- **💬 Chat** (or `C`) opens a conversation with your local Claude Code CLI. It sees the script, the dataflow map, the block you have selected and where the debugger is paused, so "what does this do?" or "why is `out` empty here?" are answered in context. Block ids and line numbers in its answers are links into the map and the editor. The conversation is kept (Claude Code session) until you press *new chat* or the file changes.
- **Debugger follow**: start the Python debugger (F5) as usual; whenever it pauses, the block being executed lights up on the map, blocks already run keep a trail, and the arrows between them are highlighted — down into a function's own flow when you step into it. **⏺ Trace** sets a breakpoint at the start of every block, so *Continue* walks the map one block at a time; switch it off to remove them.
- Blocks carry a **role**: key (changes the data) drawn in full, support (prepares a key step) folded, minor (guards, asserts, logging, counters) hidden with a count on the container — View ▾ ▸ *Hide minor blocks* or `M` shows them as slim strips. Roles come from a parser heuristic until Claude's Explain refines them.
- `main()` is drawn as a frame whose contents are the pipeline; settings and helpers sit outside it. An `if`/`match` that picks between local functions is drawn as a `?` block with one arrow per arm.
- **Open flow ▸** on any function block expands it in place into the function's own map (inputs, steps, returns); flows nest.
- Big scripts open as an **overview of sections** (from banner comments such as `# ---- Load ----`, or Claude's grouping). Click **Open ▸** on a section to drill into its blocks; **Overview / All blocks** switches levels (`O` / `A`).
- Every call to a function defined in the file becomes a block that shows the call **and** the function's code.
- Arrows follow variables: `raw = ingest_data(...)` → `preprocess_data(raw, cfg)`. Steps that don't depend on each other sit side by side.
- Maps open left-to-right; switch with **→ Horizontal / ↓ Vertical** in the toolbar (or press `D`). Default: `codeFlow.direction`.
- Drag blocks by their header; scroll to pan, ⌘/ctrl+scroll or pinch to zoom (scrolling over a code box scrolls the code); search (`/`), fit (`F`). Click a block to highlight everything upstream and downstream; click `L12` to jump to that line in the editor.
- The map refreshes when you save the file, and your layout is remembered.
- **Export HTML** saves the map — with Claude's notes and your current arrangement — as one standalone file that opens in any browser, for sharing or docs.
- **✦ Explain with Claude** asks your local Claude Code CLI for block summaries, pipeline stages and hidden dependencies (files written then read back, in-place mutation, shared globals). Without Claude Code, the parser's map still works.

## Use

Open a `.py` file and click the Code Flow icon in the editor title bar, or right-click the file → **Code Flow: Visualize Script**.

## Requirements

- Python 3.8+ on PATH (only the standard library is used), or set `codeFlow.pythonPath`.
- Optional: [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) installed and signed in, for explanations. Set `codeFlow.claudePath` if VS Code can't find it.

## Scope

Single file, Python only. No notebooks, no cross-file imports.
