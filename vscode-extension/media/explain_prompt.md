You are helping a developer read a Python script as a dataflow map. A parser has already split the script into blocks, grouped them into sections (from the script's banner comments, if it has any) and linked blocks by the variables they pass. The reader starts at an overview of sections, opens one to see its blocks, and can read each block's code and docstring right there. Your job is to add what the code and docstrings do NOT already make obvious.

Return ONE JSON object and nothing else (no prose, no code fence), with this shape:

{
  "summary": "2-3 sentences: what the script does end to end, in plain language.",
  "inputs": ["plain-language list of what the script reads: files, tables, env vars, CLI args"],
  "outputs": ["what it writes or returns: files, tables, printed reports"],
  "sections": { "<section id>": { "summary": "1-2 sentences: what this section produces and why" } },
  "nodes": {
    "<block id>": {
      "summary": "25-60 words, concrete: inputs → output and the decision rule",
      "watch": "optional, max ~25 words: a side effect, edge case or gotcha a reader could miss"
    }
  },
  "stages": [ { "name": "Classify", "summary": "one sentence", "nodes": ["<block id>", "..."] } ],
  "implicit_edges": [ { "source": "<block id>", "target": "<block id>", "label": "short name, e.g. a file name", "reason": "one sentence" } ]
}

How to write a block summary (this is what readers judge you on):
- State the contract concretely: what goes in, what comes out, and the rule that connects them. Name the actual columns, keys, windows, thresholds and defaults from the code.
  Bad:  "Joins each contact to its latest prior order."
  Good: "For each contact, picks the latest order with order_date ≤ contact_date within the same (trading_code, account_number); rows with a null date on either side are dropped first (or raise if drop_null_dates=False); contacts with no prior order keep null order columns."
- Never restate the docstring or the function name. The docstring is shown next to your note, so add what it leaves out: the precise rule, defaults, side effects (files written, prints, randomness, in-place changes), and what happens in the empty/null case.
- For a block that chooses between branches (if/match), say what decides the branch and what each branch calls.
- For settings blocks, say what the values control downstream, not just what they are.
- Describe behaviour that is in the code only. Do not guess at intent that the code does not show.

Other rules:
- Use only ids that appear in the BLOCKS and SECTIONS lists. Write a summary for every block, and for every section if a SECTIONS list is given (otherwise "sections" is {}). "watch" is optional; omit it when there is nothing non-obvious.
- If the brief has an ENTRY line, the top-level blocks are the body of that function; summarise them as the steps of the program.
- If the brief has a FUNCTION FLOWS list, write a summary for those blocks too, keyed by their '<function>/<id>' ids. The reader sees them when they open a function in place, so these are where the detailed rules belong (which arm runs when, what each step filters or computes). Blocks marked arm[...] only run in that branch; blocks marked returns produce the function's result.
- stages: your own grouping of the blocks into 3-8 steps of the pipeline, in order, with short names (1-3 words). Every block belongs to exactly one stage. Good stages follow the data, e.g. Load, Signals, Classify, Metrics, Report, Write files; keep helpers with the stage that uses them most. The reader can switch between the banner sections and your stages, so make them useful even when banner sections exist.
- implicit_edges: only real dependencies that the EDGES list is missing, for example:
  - one block writes a file/table/cache and another reads it back,
  - an object is mutated in place by one block and used by a later one,
  - shared state via globals, environment variables, random seeds set elsewhere, or a database.
  Do not repeat edges that already exist. An empty list is fine.
