# Code Flow

See a Python script as a dataflow map instead of top-to-bottom text.

- **Map | Read** switches to a plain-English walkthrough: inputs/outputs, every step in run order (branches nested), each function in depth, things to know. Built from the code alone; Claude's narratives make it read like an explanation.
- `main()` is drawn as a frame whose contents are the pipeline; settings and helpers sit outside it. An `if`/`match` that picks between local functions is drawn as a `?` block with one arrow per arm.
- **Open flow ▸** on any function block expands it in place into the function's own map (inputs, steps, returns); flows nest.
- Big scripts open as an **overview of sections** (from banner comments such as `# ---- Load ----`, or Claude's grouping). Click **Open ▸** on a section to drill into its blocks; **Overview / All blocks** switches levels (`O` / `A`).
- Every call to a function defined in the file becomes a block that shows the call **and** the function's code.
- Arrows follow variables: `raw = ingest_data(...)` → `preprocess_data(raw, cfg)`. Steps that don't depend on each other sit side by side.
- Maps open left-to-right; switch with **→ Horizontal / ↓ Vertical** in the toolbar (or press `D`). Default: `codeFlow.direction`.
- Drag blocks by their header; scroll to pan, ⌘/ctrl+scroll or pinch to zoom (scrolling over a code box scrolls the code); search (`/`), fit (`F`). Click a block to highlight everything upstream and downstream; click `L12` to jump to that line in the editor.
- The map refreshes when you save the file, and your layout is remembered.
- **✦ Explain with Claude** asks your local Claude Code CLI for block summaries, pipeline stages and hidden dependencies (files written then read back, in-place mutation, shared globals). Without Claude Code, the parser's map still works.

## Use

Open a `.py` file and click the Code Flow icon in the editor title bar, or right-click the file → **Code Flow: Visualize Script**.

## Requirements

- Python 3.8+ on PATH (only the standard library is used), or set `codeFlow.pythonPath`.
- Optional: [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) installed and signed in, for explanations. Set `codeFlow.claudePath` if VS Code can't find it.

## Scope

Single file, Python only. No notebooks, no cross-file imports.
