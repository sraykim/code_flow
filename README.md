# Code Flow

Read a Python script as a map, not a scroll. Each call to a function defined in
the file becomes a block holding the call **and** the function's code; arrows
follow the variables passed between them, so independent steps sit side by side.

```
ingest_data ──raw──┐
                   ├──► preprocess_data ──clean──► print(...)
read_config ──cfg──┘
```

`main()` is drawn as a frame whose contents are the pipeline, an `if`/`match` that chooses between local functions
is drawn as a fork, and any function block can be opened in place into its own map
(**Open flow ▸**), as deep as the calls go. Maps open left-to-right.

A **Read** tab turns the same graph into a step-by-step walkthrough in plain English.

Big scripts open as an **overview**: one card per section (from banner comments like
`# ---- Load ----`, or Claude's own grouping), joined only by the variables that cross
sections. Open a section to see its blocks; open a block to read its code. Values used
almost everywhere (`CAT_META`, `fmt_int()`) become tags instead of arrows, loops stay
as one block, and loop temporaries fold into a "+N local" tag.

Two front ends share one core:

| | What it is | Intelligence |
|---|---|---|
| `plugins/code-flow` | Claude Code plugin (`/code-flow:code-flow script.py`) → standalone HTML page | Claude Code itself writes summaries, stages, hidden links |
| `vscode-extension` | VS Code panel beside your editor | "✦ Explain with Claude" runs your local `claude -p`; works without it |

## Install

**Claude Code (recommended): from this repo as a plugin marketplace**
```
claude plugin marketplace add sraykim/code_flow
claude plugin install code-flow@code-flow
```
Then in any session: `/code-flow:code-flow path/to/script.py` (or "map the flow of train.py").
Updates: `claude plugin marketplace update code-flow` then `claude plugin update code-flow@code-flow`.

**Other ways**

**VS Code extension**
```
code --install-extension dist/code-flow-0.5.0.vsix
```
Open a `.py` file → click the Code Flow icon in the editor title bar (or right-click → *Code Flow: Visualize Script*).
Needs Python 3.8+ (standard library only). For explanations, Claude Code must be installed and signed in;
set `codeFlow.claudePath` if VS Code can't find it.

**Claude Code skill without the marketplace**
```
cp -R plugins/code-flow/skills/code-flow ~/.claude/skills/
```
Then `/code-flow path/to/script.py`.

**Without either** (deterministic only):
```
python3 core/codeflow_render.py path/to/script.py --open
```

## How it works

1. `core/codeflow_parse.py`: Python `ast`, standard library only. Walks the pipeline (top level, or the body of `main()`) in order and
   tracks which block last wrote each variable (branch-aware: after an `if`/`try`, a variable may come from either
   branch). Produces blocks: **fn** (call to a local function), **code**, **set** (literal settings),
   **flow** (loop/branch header), **helper** (only called inside other functions), **unused**.
   Globals read inside a function become dashed edges. `--brief` prints a compact list for Claude.
2. Claude (optional) reads the brief + source and returns JSON following `core/explain_prompt.md`:
   block summaries, 2–7 stages, and `implicit_edges`, the dependencies static analysis can't see
   (file written then read, in-place mutation, shared state).
3. `core/viewer.html`: one self-contained page, no dependencies, works offline. Layered layout (stages
   become horizontal bands), draggable blocks, pan/zoom, search, lineage highlight, VS Code theme aware.

## Rebuild

Edit files in `core/`, then `./build.sh` copies them into the skill and extension and repackages `dist/`.

## Limits and next steps

- Single file, Python only.
- In-place mutation (`df.dropna(inplace=True)`) and attribute state (`self.x`) aren't tracked by the parser;
  Claude's implicit edges cover the important cases.
- Each call site is its own block. Loops stay whole; open the block's code to read the loop.
- Ideas: notebooks (cells → blocks), follow imports one level, parse the unsaved editor buffer, export PNG/SVG.
