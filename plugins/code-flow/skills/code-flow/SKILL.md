---
name: code-flow
description: Turn a single Python script into an interactive dataflow map - an overview of its sections that opens into draggable code blocks linked by the variables they pass, with Claude's summaries, stages and hidden dependencies. Use when the user asks to visualize, map, diagram or "help me understand the flow of" a Python script, or runs /code-flow (installed as a plugin: /code-flow:code-flow).
---

# Code Flow

Shows a Python script as a map instead of top-to-bottom text. Large scripts open
as an **overview**: one card per section (found from the script's banner
comments, e.g. `# ---- Load ----`), linked by the variables that cross between
sections. Opening a section shows its blocks: each call to a function defined in
the file (with that function's code), plain top-level code (loops stay whole),
settings and helpers. Arrows follow variables (`raw`, `cfg`, ...), so steps that
don't depend on each other sit side by side. Values used almost everywhere
(e.g. `CAT_META`, `fmt_int()`) are "lookups": shown as tags, not arrows.
The page is a single HTML file the user can pan, zoom, drag and search.

Three things the parser does that matter for reading the map:

- **Entry function.** `main()` (or the one function called once at top level,
  e.g. `sys.exit(main())`) is drawn as a blue frame whose contents are the
  pipeline; module-level settings and helpers sit outside it. The header says
  `pipeline = main()`.
- **Branches.** An `if`/`elif`/`else` or `match` whose arms call local
  functions becomes a `?` block with one teal arrow per arm, so the reader
  sees which rule runs when. Plain ifs and all loops stay inside one block.
- **Function flows.** Any function with a body worth reading gets
  **Open flow ▸**: the block expands in place into the function's own map
  (an Inputs block for its parameters, its statements as blocks, `return`
  blocks marked). Flows nest, so a function called inside an opened function
  can be opened too. Blocks inside an opened flow start with their code folded.

Scope: one `.py` file, Python only. No notebooks, no following imports into
other files.

The scripts live in the `scripts/` folder of this skill's directory. Below,
`SKILL_DIR` means that directory.

## Steps

1. **Find the script.** Use the path the user gave (or the file they're
   working on). If it isn't a `.py` file, say so and stop.

2. **Parse it (deterministic).**
   ```bash
   python3 "SKILL_DIR/scripts/codeflow_parse.py" path/to/script.py --brief
   ```
   This prints the block ids, what each block reads and writes, and the edges
   the parser found. If it prints a `SyntaxError`, tell the user the line and stop.

3. **Read the script yourself** (Read tool) with the brief beside it.

4. **Write the enrichment** (this is the intelligence layer). Follow
   `SKILL_DIR/reference/explain_prompt.md` exactly, and add a top-level
   `"source_hash"` copied from the brief so stale notes are ignored after the
   script changes. Save it as `<script dir>/.codeflow/<script stem>.enrich.json`.
   Give every block a `role`: `key` (changes the data the result depends on),
   `support` (prepares something for a key block) or `minor` (guards, asserts,
   logging, prints, counters). The brief carries the parser's guess as
   `role?=`; override it where the code says otherwise. Key-block summaries
   must be concrete (inputs → output and the rule, with the real column names,
   keys, windows and defaults), never a restatement of the docstring, which is
   shown beside your note; support blocks get one short sentence, minor blocks
   a few words. Use `watch` for a gotcha.
   Also cover the blocks listed under FUNCTION FLOWS (ids like
   `attribute_by_erc/n9`): that is where the detailed rules belong, since the
   reader sees them when they open the function. Write a summary for every
   section listed under SECTIONS (they appear on the overview cards) and
   propose your own `stages`, which the reader can switch to under View ▸
   Group sections by ▸ Claude's stages; that matters most when the script
   has no banner comments or misleading ones.
   Focus the rest of your effort on `implicit_edges`: files written then read back, objects
   mutated in place, globals or seeds set in one place and relied on elsewhere.
   These are what the parser can't see and what readers most often miss.

   Skip this step if the user asked for a quick/plain map; the page works without it.

5. **Render.**
   ```bash
   python3 "SKILL_DIR/scripts/codeflow_render.py" path/to/script.py \
     --enrich path/to/.codeflow/<stem>.enrich.json --open
   ```
   Maps open left-to-right; add `--vertical` for top-to-bottom (switchable
   in the page either way). It prints the output path (default `<script dir>/.codeflow/<stem>.flow.html`)
   and opens it in the browser. Renderer warnings about unknown ids mean your
   JSON used an id that isn't in the brief; fix and re-render.

6. **Reply briefly**: the path to the page, then one or two sentences on
   anything a reader would miss from the code alone (usually the implicit
   edges). Don't restate the whole graph; the page shows it.

## Using the page

When the user wants the script explained in prose, do it here in the
conversation with the brief and the script in hand — the page is for the map.
(In VS Code the same page has a **💬 Chat** panel that talks to Claude Code
with the map in view, and the map follows the Python debugger block by block.)

Start from the overview: click **Open ▸** on a section card (or a line in its
list) to see its blocks, **Collapse ▴** to fold it back, **Overview / All blocks**
for everything at once (`O` / `A`). **Open flow ▸** on a function block shows
the function's own map in place; **Collapse ▴** on its frame closes it. Drag blocks and cards by their header;
scroll (or drag the background) to pan; ⌘/ctrl+scroll, pinch or `+`/`-` to zoom.
Key blocks are drawn in full, support blocks folded to their chips, and minor
blocks (guards, logging) are hidden — the container header counts them, and
View ▾ ▸ Hide minor blocks (or `M`) shows them as slim strips.
Scrolling over a code panel scrolls the code, in both directions. Global reads
inside functions show as tags by default; View ▾ can draw them as arrows. **↓ Vertical / → Horizontal** (or `D`)
switches direction; each direction remembers its own arrangement. Click a block to highlight everything
upstream and downstream; click a variable chip to jump to where it comes from
or goes. `L12` links open the line in VS Code. `/` searches, `F` fits, `Tidy`
re-runs the layout. Positions are remembered per file in the browser.

## Notes

- Every call site is its own block, so a function called twice appears twice.
- Functions only called from inside other functions appear as purple helper
  blocks; never-called functions are shown dashed.
- Dashed grey arrows are module-level globals read inside a function.
- `.codeflow/` can be added to `.gitignore`.
