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

Blocks have a **role** so the map reads at human weight, not AST weight: key blocks (joins, filters that define
the output, writes, calls) are drawn in full, support blocks folded, and minor ones (guards, asserts, logging,
counters) hidden behind a count until you ask for them (`M`). The parser guesses roles; Explain refines them
and sizes its notes accordingly.

**Imports and SQL.** Functions imported from the project's own modules are read by the parser and appear as
real step blocks with their code and an openable flow, tagged with the file they come from; third-party
packages are listed with version, description and the blocks that use them (and Claude's note on what the
script uses them for). SQL in strings, f-strings, `.sql` files and `%%sql`/`%%bigquery` cells is parsed:
blocks show the tables they read and write, **Open query ▸** expands a query into one block per CTE, a
table written by one block and read by another becomes a dash-dot arrow, and a standalone `.sql` file opens
as its own CTE map. No SQL dependency is needed; `sqlglot`, if installed, sharpens the table list.

Jupyter notebooks (`.ipynb`) work the same way: code cells are read in document order, markdown headings
become sections, each cell's output (text, tables, images, errors) is shown under its block, line links
point at cells, and the map flags the two notebook traps — cells run out of document order, and names used
before the cell that defines them (dashed pink arrow).

In VS Code, **💬 Chat** opens a conversation with Claude Code that sees the script, the map
and whatever you have selected, and **⏺ Trace** makes the map follow the Python debugger:
the block being executed lights up, the blocks already run keep a trail, and the arrows taken
are highlighted, down into opened functions.

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
code --install-extension dist/code-flow-0.9.0.vsix
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
