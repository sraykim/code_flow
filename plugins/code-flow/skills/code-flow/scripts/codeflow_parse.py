#!/usr/bin/env python3
"""Code Flow parser: turn a single Python script into a dataflow graph (JSON).

The script is read as a pipeline of top-level statements (no main() needed).
Every call to a function defined in the same file becomes a "step" block that
carries that function's source. Plain top-level code becomes "code" blocks,
literal settings become "value" blocks, and functions that are only used from
inside other functions become "helper" blocks. Edges follow variables: if a
block writes `df` and a later block reads `df`, there is an edge labelled `df`
between them. Blocks with no dependency on each other end up side by side.

Usage:
    python codeflow_parse.py script.py [-o graph.json]

Only the standard library is used, so it runs with any Python 3.8+.
"""
from __future__ import annotations

import argparse
import ast
import builtins
import hashlib
import json
import re
import sys
from pathlib import Path

SCHEMA_VERSION = 4
BUILTIN_NAMES = set(dir(builtins))


# --------------------------------------------------------------------------
# Name collection
# --------------------------------------------------------------------------

# ---------- block importance (a human-reading heuristic, not an AST one) ----------
SIDE_FUNCS = {"print", "pprint", "warn", "warning", "info", "debug", "error", "exception", "critical", "log",
              "setLevel", "basicConfig", "display", "echo", "secho", "describe", "head", "tail", "show"}
SIDE_OBJECTS = {"logger", "log", "logging", "warnings", "console", "st", "click", "typer"}
WRITE_METHODS = {"to_csv", "to_parquet", "to_excel", "to_json", "to_pickle", "to_sql", "to_feather", "to_hdf",
                 "write_parquet", "write_csv", "write_json", "write_ipc", "write_excel", "write_database", "write_delta",
                 "savefig", "save", "save_pretrained", "dump", "write", "writelines", "write_text", "write_bytes",
                 "put", "upload_file", "upload_fileobj", "commit", "executemany", "insert_many", "to_gbq", "copy_to"}


def _side_stmt(st) -> bool:
    """True for statements a reader skims past: prints, logging, asserts, raises, guards that only do those."""
    if isinstance(st, (ast.Pass, ast.Continue, ast.Break, ast.Raise, ast.Assert)):
        return True
    if isinstance(st, ast.Return) and st.value is None:
        return True
    if isinstance(st, ast.Expr):
        v = st.value
        if isinstance(v, ast.Await):
            v = v.value
        if isinstance(v, ast.Call):
            f = v.func
            if isinstance(f, ast.Name):
                return f.id in SIDE_FUNCS
            if isinstance(f, ast.Attribute):
                base = f.value
                while isinstance(base, ast.Attribute):
                    base = base.value
                return f.attr in SIDE_FUNCS or (isinstance(base, ast.Name) and base.id in SIDE_OBJECTS)
        return isinstance(v, ast.Constant)  # a stray docstring / string literal
    if isinstance(st, ast.If):
        return all(_side_stmt(x) for x in st.body + st.orelse)
    if isinstance(st, ast.Try):
        return (all(_side_stmt(x) for x in st.body + st.orelse + st.finalbody)
                and all(_side_stmt(x) for h in st.handlers for x in h.body))
    if isinstance(st, ast.With):
        return all(_side_stmt(x) for x in st.body)
    return False


def _writes_file(stmts) -> bool:
    for st in stmts:
        for x in ast.walk(st):
            if isinstance(x, ast.Call):
                f = x.func
                if isinstance(f, ast.Attribute) and f.attr in WRITE_METHODS:
                    return True
                if isinstance(f, ast.Name) and f.id == "open":
                    for a in x.args[1:2] + [k.value for k in x.keywords if k.arg == "mode"]:
                        if isinstance(a, ast.Constant) and isinstance(a.value, str) and any(c in a.value for c in "wax"):
                            return True
    return False


def assign_roles(nodes, edges):
    """key: changes the data (a local-function step, a branch, a block whose result feeds several
    others, is returned, or is written out). minor: guards, logging, counters nobody reads.
    support: everything else. Claude may override these in its enrichment."""
    consumers = {}
    for e in edges:
        if e["kind"] in ("data", "global", "branch", "forward", "table"):
            consumers.setdefault(e["source"], set()).add(e["target"])
    for n in nodes:
        k = n["kind"]
        writes_file = n.pop("_writes_file", False)
        side_only = n.pop("_side_only", False)
        if k == "step":
            role = "key"
        elif k == "branch":
            role = "key" if len(n.get("arms") or []) >= 2 else "support"   # a lone guard is not a decision
        elif k == "unused":
            role = "minor"
        elif k in ("helper", "value", "input"):
            role = "support"
        elif writes_file:
            role = "key"
        elif n.get("sql"):
            used = len(consumers.get(n["id"], ()))
            role = "key" if (n["sql"]["writes"] or n.get("returns") or used >= 1) else "support"
        elif side_only:
            role = "minor"
        else:
            used = len(consumers.get(n["id"], ()))
            if n.get("returns") or used >= 2:
                role = "key"
            elif used == 0:
                role = "minor"
            else:
                role = "support"
        n["role"] = role


class NameCollector(ast.NodeVisitor):
    """Collect variable names read and written by an expression/statement.

    Names bound inside comprehensions and lambdas are local to them and are
    not reported. Subtrees listed in `skip` are not visited (used to leave out
    nested calls that become their own blocks).
    """

    def __init__(self, skip=None):
        self.reads: dict[str, None] = {}
        self.writes: dict[str, None] = {}
        self.skip = skip or set()

    def visit(self, node):
        if id(node) in self.skip:
            return
        return super().visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.reads.setdefault(node.id)
        else:
            self.writes.setdefault(node.id)

    def _base_name(self, node):
        while isinstance(node, (ast.Attribute, ast.Subscript, ast.Starred)):
            node = node.value
        return node.id if isinstance(node, ast.Name) else None

    def _mutation(self, node):
        # `df["a"] = ...` or `obj.attr = ...` reads and changes `df` / `obj`.
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            base = self._base_name(node)
            if base:
                self.reads.setdefault(base)
                self.writes.setdefault(base)
        self.generic_visit(node)

    visit_Attribute = _mutation
    visit_Subscript = _mutation

    # Calls that change an object in place: `rows.append(x)` as a statement,
    # or pandas-style `df.dropna(inplace=True)`.
    MUTATORS = {"append", "extend", "insert", "update", "add", "pop", "popitem", "remove",
                "clear", "setdefault", "discard", "sort", "reverse", "appendleft", "extendleft"}

    def _mark_mutation(self, call):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
            base = self._base_name(call.func.value)
            if base:
                self.reads.setdefault(base)
                self.writes.setdefault(base)

    def visit_Expr(self, node):
        v = node.value
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr in self.MUTATORS:
            self._mark_mutation(v)
        self.generic_visit(node)

    def visit_Call(self, node):
        if any(k.arg == "inplace" and isinstance(k.value, ast.Constant) and k.value.value is True
               for k in node.keywords):
            self._mark_mutation(node)
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        base = self._base_name(node.target) if not isinstance(node.target, ast.Name) else node.target.id
        if base:
            self.reads.setdefault(base)
            self.writes.setdefault(base)
        self.visit(node.value)
        if not isinstance(node.target, ast.Name):
            self.visit(node.target)

    def _scoped(self, node, bound):
        inner = NameCollector(self.skip)
        inner.generic_visit(node)
        for n in inner.reads:
            if n not in bound:
                self.reads.setdefault(n)
        for n in inner.writes:
            if n not in bound:
                self.writes.setdefault(n)

    def _comp(self, node):
        bound = set()
        for gen in node.generators:
            for t in ast.walk(gen.target):
                if isinstance(t, ast.Name):
                    bound.add(t.id)
        self._scoped(node, bound)

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _comp

    def visit_Lambda(self, node):
        self._scoped(node, set(_arg_names(node.args)))

    # Nested definitions are handled separately.
    def visit_FunctionDef(self, node):
        self.writes.setdefault(node.name)

    visit_AsyncFunctionDef = visit_ClassDef = visit_FunctionDef


def collect(node, skip=None) -> NameCollector:
    c = NameCollector(skip)
    c.visit(node)
    return c


_COMPOUND_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")


def stmt_rw(st, written=None):
    """Reads/writes of a statement, walking compound bodies in order so that a
    name assigned inside an `if`/`for` body and used afterwards in the same body
    is not reported as an outside input."""
    written = set() if written is None else written
    reads, writes = {}, {}

    def absorb(c):
        for r in c.reads:
            if r not in written:
                reads.setdefault(r)
        for w in c.writes:
            writes.setdefault(w)
            written.add(w)

    if not any(isinstance(getattr(st, f, None), list) and getattr(st, f) and
               isinstance(getattr(st, f)[0], (ast.stmt, ast.excepthandler, getattr(ast, "match_case", ast.stmt)))
               for f in _COMPOUND_FIELDS):
        absorb(collect(st))
        return reads, writes

    # Header expressions first (test / iter / target / with items / subject).
    for field, value in ast.iter_fields(st):
        if field in _COMPOUND_FIELDS:
            continue
        for v in value if isinstance(value, list) else [value]:
            if isinstance(v, ast.AST):
                absorb(collect(v))
    for field in _COMPOUND_FIELDS:
        for child in getattr(st, field, None) or []:
            if isinstance(child, ast.excepthandler) and child.name:
                written.add(child.name)
            body = child.body if isinstance(child, ast.AST) and not isinstance(child, ast.stmt) else None
            for sub in (body if body is not None else [child]):
                r, w = stmt_rw(sub, written)
                for n in r:
                    reads.setdefault(n)
                for n in w:
                    writes.setdefault(n)
    return reads, writes


_SCALAR_TYPES = re.compile(r"^(str|int|float|bool|bytes|Path|pathlib\.Path|None)$")


def _is_config_type(ann: str | None) -> bool:
    """Annotation looks like a setting (str, int, list[str], str | None, ...),
    not a data object (DataFrame, dict of records, model, ...)."""
    if not ann:
        return False
    parts = [p.strip() for p in ann.replace("Optional[", "").replace("]", "").split("|")]
    parts = [p for p in parts if p]
    ok = bool(parts)
    for p in parts:
        inner = p
        for wrap in ("list[", "tuple[", "frozenset[", "set[", "Sequence[", "Iterable[", "List[", "Tuple[", "FrozenSet[", "Set["):
            if inner.startswith(wrap):
                inner = inner[len(wrap):]
        inner = inner.split(",")[0].strip()
        ok = ok and bool(_SCALAR_TYPES.match(inner))
    return ok


def _arg_types(args: ast.arguments):
    out = {}
    for a in list(getattr(args, "posonlyargs", [])) + list(args.args) + list(args.kwonlyargs):
        if a.annotation is not None:
            try:
                out[a.arg] = ast.unparse(a.annotation)
            except Exception:  # pragma: no cover
                pass
    return out


def _arg_names(args: ast.arguments):
    names = [a.arg for a in getattr(args, "posonlyargs", [])]
    names += [a.arg for a in args.args]
    names += [a.arg for a in args.kwonlyargs]
    if args.vararg:
        names.append(args.vararg.arg)
    if args.kwarg:
        names.append(args.kwarg.arg)
    return names


# ---------- Jupyter notebooks ----------
# A notebook is read as one virtual script: the code cells in document order, each followed by a
# blank line (so a cell never merges with the next), markdown headings turned into section banners,
# and IPython-only syntax commented out so `ast` can parse it. Every virtual line maps back to a cell.
NB_FOREIGN_MAGICS = {"bash", "sh", "script", "sql", "html", "javascript", "js", "latex", "markdown", "ruby",
                     "perl", "writefile", "svg", "file", "R", "cython", "pypy", "bigquery", "spark"}
NB_TEXT_LINES, NB_TEXT_CHARS, NB_IMAGE_BUDGET = 20, 3000, 400_000
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_HELP = re.compile(r"^\s*[\w.\[\]()'\"]+\?{1,2}\s*$")


NB_SQL_MAGICS = {"sql", "bigquery", "spark", "sparksql", "snowflake", "duckdb", "athena", "presto", "trino", "hive",
                 "clickhouse", "mysql", "postgres", "postgresql", "sqlite", "redshift", "teradata", "synapse"}


def _clean_ipython(body):
    """Comment out magics, shell escapes and `obj?` help lines, keeping line count and indentation.
    A %%sql-style cell becomes `result = r'''<query>'''` so the SQL is found like any other string."""
    foreign = False
    if body and body[0].lstrip().startswith("%%"):
        words = body[0].lstrip()[2:].split()
        if words and words[0].lower() in NB_SQL_MAGICS:
            target = next((w for w in words[1:] if re.fullmatch(r"[A-Za-z_]\w*", w)), None) or "_sql_result"
            q = "'''" if "'''" not in "\n".join(body) else '"""'
            rest = body[1:]
            if not rest:
                return [f"{target} = {q}{q}  #> {body[0].strip()}"]
            return [f"{target} = r{q}  #> {body[0].strip()}"] + rest[:-1] + [rest[-1].rstrip("\n") + q]
        foreign = bool(words) and words[0] in NB_FOREIGN_MAGICS
    out = []
    for line in body:
        stripped = line.lstrip()
        if foreign or stripped.startswith(("%", "!")) or _HELP.match(line):
            indent = line[: len(line) - len(stripped)]
            out.append((indent + "pass  #> " + stripped.rstrip()) if indent else "#> " + stripped.rstrip())
        else:
            out.append(line.rstrip("\n"))
    return out


def _nb_text(x) -> str:
    return "".join(x) if isinstance(x, list) else str(x or "")


def _truncate(text: str) -> str:
    lines = text.splitlines()
    more = len(lines) - NB_TEXT_LINES
    lines = [l if len(l) <= 200 else l[:199] + "…" for l in lines[:NB_TEXT_LINES]]
    out = "\n".join(lines)
    if len(out) > NB_TEXT_CHARS:
        out = out[:NB_TEXT_CHARS] + "…"
    if more > 0:
        out += f"\n… {more} more lines"
    return out


def _strip_tags(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    text = re.sub(r"</t[dh]>", "\t", text, flags=re.I)
    text = re.sub(r"</tr>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _collect_outputs(cell, budget: dict) -> list:
    outs = []
    for o in cell.get("outputs") or []:
        t = o.get("output_type")
        if t == "stream":
            text = _nb_text(o.get("text"))
            if text.strip():
                outs.append({"kind": "error" if o.get("name") == "stderr" else "text", "text": _truncate(text)})
        elif t in ("execute_result", "display_data"):
            d = o.get("data") or {}
            if "image/png" in d:
                b64 = _nb_text(d["image/png"]).replace("\n", "")
                if len(b64) <= budget["images"]:
                    budget["images"] -= len(b64)
                    outs.append({"kind": "image", "data": "data:image/png;base64," + b64})
                else:
                    outs.append({"kind": "text", "text": "[image omitted: notebook image budget used up]"})
            elif "text/plain" in d:
                outs.append({"kind": "text", "text": _truncate(_nb_text(d["text/plain"]))})
            elif "text/html" in d:
                outs.append({"kind": "text", "text": _truncate(_strip_tags(_nb_text(d["text/html"])))})
            elif "application/json" in d:
                outs.append({"kind": "text", "text": _truncate(json.dumps(d["application/json"], indent=1))})
        elif t == "error":
            tb = [_ANSI.sub("", l) for l in (o.get("traceback") or [])]
            tail = "\n".join(tb[-4:])
            outs.append({"kind": "error", "text": _truncate(f"{o.get('ename', 'Error')}: {o.get('evalue', '')}\n{tail}".strip())})
    return outs[:6]


def load_notebook(path: str):
    nb = json.loads(Path(path).read_text(encoding="utf-8"))
    cells, lines, heading, md_buf = [], [], None, []
    budget = {"images": NB_IMAGE_BUDGET}
    exec_max, out_of_order, n, doc = 0, [], 0, ""
    for idx, c in enumerate(nb.get("cells") or []):
        src = _nb_text(c.get("source"))
        kind = c.get("cell_type")
        if kind == "markdown":
            for ln in src.splitlines():
                m = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", ln)
                if m:
                    heading, md_buf = m.group(1).strip(), []
                elif ln.strip():
                    md_buf.append(ln.strip())
            if not doc and not cells and md_buf:
                doc = " ".join(md_buf)[:600]
            continue
        if kind != "code":
            continue
        n += 1
        ec = c.get("execution_count")
        start = len(lines) + 1
        if heading is not None:
            lines.append(f"# --- {heading} ---")
            heading = None
        code_start = len(lines) + 1
        lines.extend(_clean_ipython(src.splitlines()))
        end = len(lines)
        lines.append("")
        if isinstance(ec, int):
            if ec < exec_max:
                out_of_order.append(n)
            exec_max = max(exec_max, ec)
        cells.append({"n": n, "index": idx, "id": c.get("id"), "exec": ec if isinstance(ec, int) else None,
                      "lines": [start, max(end, code_start)], "code_start": code_start,
                      "md": " ".join(md_buf)[:400], "outputs": _collect_outputs(c, budget)})
        md_buf = []
    source = "\n".join(lines) + "\n"
    return source, {"cells": cells, "code_cells": n, "out_of_order": out_of_order, "doc": doc}


def cell_of(nbinfo: dict, line: int):
    for c in nbinfo["cells"]:
        if c["lines"][0] <= line <= c["lines"][1]:
            return c
    return None


# ---------- SQL inside the code ----------
# SQL lives in strings: a literal passed to read_sql / execute / query, an f-string template, a `.sql`
# file read from disk, a %%sql cell. We find it, split it into CTEs (each becomes a block when the
# query is opened in place) and extract the tables it reads and writes, so table lineage between
# blocks can be drawn without Claude guessing. Stdlib regexes do the work; sqlglot sharpens the
# table list when it happens to be installed.
SQL_START = re.compile(r"^\s*(?:--[^\n]*\n\s*|#[^\n]*\n\s*|/\*.*?\*/\s*)*(WITH|SELECT|INSERT|UPDATE|DELETE|CREATE|MERGE|ALTER|DROP|TRUNCATE|REPLACE|COPY|GRANT|CALL|EXPORT)\b", re.I | re.S)
SQL_SINKS = {"read_sql", "read_sql_query", "read_sql_table", "read_database", "read_database_uri", "execute", "executemany",
             "query", "sql", "run_query", "query_and_wait", "text", "raw_sql", "execute_query", "run"}
SQL_KEYWORDS_NOT_TABLES = {"UNNEST", "LATERAL", "SELECT", "VALUES", "TABLE", "ONLY", "GENERATE_SERIES", "GENERATE_ARRAY",
                           "JSON_TABLE", "DUAL", "EXTERNAL_QUERY", "ML", "TABLESAMPLE", "READ_PARQUET", "READ_CSV", "READ_CSV_AUTO", "READ_JSON"}
_TBL = r"((?:[`\"\[]?[\w$\-{}]+[`\"\]]?\s*\.\s*){0,2}[`\"\[]?[\w$\-{}]+[`\"\]]?)"   # {name} keeps f-string placeholders
_READ_RE = re.compile(r"\b(?:FROM|JOIN)\s+(?!\()" + _TBL + r"(?![\w$])(?!\s*\()", re.I)
_WRITE_RES = [
    re.compile(r"\bINSERT\s+(?:OR\s+\w+\s+)?(?:INTO|OVERWRITE)\s+(?:TABLE\s+)?" + _TBL, re.I),
    re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+|TRANSIENT\s+|EXTERNAL\s+|MATERIALIZED\s+)?(?:TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?" + _TBL, re.I),
    re.compile(r"\bUPDATE\s+(?:ONLY\s+)?" + _TBL, re.I),
    re.compile(r"\bDELETE\s+FROM\s+(?:ONLY\s+)?" + _TBL, re.I),
    re.compile(r"\bMERGE\s+(?:INTO\s+)?" + _TBL, re.I),
    re.compile(r"\bTRUNCATE\s+(?:TABLE\s+)?" + _TBL, re.I),
    re.compile(r"\bDROP\s+(?:TABLE|VIEW)\s+(?:IF\s+EXISTS\s+)?" + _TBL, re.I),
    re.compile(r"\bCOPY\s+(?:INTO\s+)?" + _TBL, re.I),
]
_CTE_HEAD = re.compile(r"([`\"\[]?[\w$]+[`\"\]]?)\s*(?:\([^)]*\)\s*)?AS\s*\(", re.I)


def _blank_comments(sql: str) -> str:
    """Replace comments and string literals with spaces, keeping every character position."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("--", i) or sql.startswith("#", i) and (i == 0 or sql[i - 1] in " \n\t"):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i)); i = j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", sql[i:j])); i = j
        elif c == "'":   # single quotes are strings; double quotes are identifiers in most dialects, keep them
            j = i + 1
            while j < n and sql[j] != c:
                j += 2 if sql[j] == "\\" else 1
            j = min(n, j + 1)
            out.append(c + re.sub(r"[^\n]", " ", sql[i + 1:j - 1]) + (c if j - i > 1 else "")); i = j
        else:
            out.append(c); i += 1
    return "".join(out)


def _clean_name(t: str) -> str:
    return re.sub(r"[`\"\[\]]", "", re.sub(r"\s+", "", t))


def _tables(sql_clean: str, exclude: set) -> list:
    out = []
    for m in _READ_RE.finditer(sql_clean):
        name = _clean_name(m.group(1))
        head = name.split(".")[0].upper()
        if head in SQL_KEYWORDS_NOT_TABLES or name.upper() in exclude or not name or name[0].isdigit():
            continue
        if name not in out:
            out.append(name)
    return out


def _write_targets(sql_clean: str) -> list:
    out = []
    for rx in _WRITE_RES:
        for m in rx.finditer(sql_clean):
            name = _clean_name(m.group(1))
            if name and name.upper() not in SQL_KEYWORDS_NOT_TABLES and name not in out:
                out.append(name)
    return out


def _match_paren(s: str, i: int) -> int:
    """Index just past the ')' matching the '(' at i (s has strings/comments blanked)."""
    depth, n = 0, len(s)
    while i < n:
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _split_ctes(clean: str):
    """[(name, start, end)] for each CTE body (offsets into the text) and the offset where the final statement starts."""
    m = re.match(r"\s*WITH\s+(?:RECURSIVE\s+)?", clean, re.I)
    if not m:
        return [], 0
    pos, ctes = m.end(), []
    while True:
        h = _CTE_HEAD.match(clean, pos)
        if not h:
            break
        start = h.end()
        end = _match_paren(clean, start - 1)
        ctes.append((_clean_name(h.group(1)), start, end - 1))
        pos = end
        c = re.match(r"\s*,\s*", clean[pos:])
        if not c:
            break
        pos += c.end()
    return ctes, pos


def _dialect_hint(sql: str) -> str:
    if re.search(r"`[\w{}-]+\.[\w{}-]+\.[\w{}-]+`|`[\w{}-]+\.[\w{}-]+`|\bSAFE_CAST\b|\bUNNEST\b|\bQUALIFY\b.*\bROW_NUMBER", sql, re.I | re.S):
        return "bigquery"
    if re.search(r"\bQUALIFY\b|\bFLATTEN\s*\(|\bVARIANT\b|\bILIKE\b", sql, re.I):
        return "snowflake"
    if re.search(r"::\w+|\bILIKE\b|\bRETURNING\b|\bNOW\(\)", sql, re.I):
        return "postgres"
    if re.search(r"\bread_parquet\s*\(|\bread_csv\w*\s*\(", sql, re.I):
        return "duckdb"
    return ""


def parse_sql(text: str) -> dict:
    """Tables read/written, CTEs with what each reads, and the final statement."""
    text = re.sub(r"\{\{\s*ref\(\s*['\"]([\w.]+)['\"]\s*\)\s*\}\}", r"\1", text)
    text = re.sub(r"\{\{\s*source\(\s*['\"]([\w.]+)['\"]\s*,\s*['\"]([\w.]+)['\"]\s*\)\s*\}\}", r"\1.\2", text)
    clean = _blank_comments(text)
    ctes, final_at = _split_ctes(clean)
    names = {c[0].upper() for c in ctes}
    line_at = lambda off: clean.count("\n", 0, off)
    out_ctes = []
    for name, a, b in ctes:
        body = clean[a:b]
        reads = _tables(body, set())
        out_ctes.append({
            "name": name, "lines": [line_at(a), line_at(b)], "code": text[a:b].strip("\n"),
            "reads_ctes": [r for r in reads if r.upper() in names],
            "reads": [r for r in reads if r.upper() not in names],
            "writes": _write_targets(body),
        })
    final_clean, final_text = clean[final_at:], text[final_at:]
    reads = _tables(final_clean, set())
    kw = re.match(r"\s*(\w+)", final_clean)
    final = {"kind": (kw.group(1).upper() if kw else "SELECT"), "lines": [line_at(final_at), text.count("\n")],
             "code": final_text.strip("\n"), "reads_ctes": [r for r in reads if r.upper() in names],
             "reads": [r for r in reads if r.upper() not in names], "writes": _write_targets(final_clean)}
    all_reads = list(dict.fromkeys(r for c in out_ctes + [final] for r in c["reads"]))
    all_writes = list(dict.fromkeys(w for c in out_ctes + [final] for w in c["writes"]))
    try:  # sqlglot, when installed, knows the dialects better than our regexes
        import sqlglot  # type: ignore
        from sqlglot import exp  # type: ignore
        tree = sqlglot.parse_one(clean, read=_dialect_hint(text) or None, error_level=sqlglot.ErrorLevel.IGNORE)
        seen = []
        for t in tree.find_all(exp.Table):
            full = ".".join(p for p in (t.catalog, t.db, t.name) if p)
            if full and full.upper() not in names and full not in seen:
                seen.append(full)
        if seen:
            all_reads = [r for r in seen if r not in all_writes] or all_reads
    except Exception:
        pass
    return {"dialect": _dialect_hint(text), "kind": final["kind"], "reads": all_reads, "writes": all_writes,
            "ctes": out_ctes, "final": final, "lines": text.count("\n") + 1}


def is_sql_text(text: str) -> bool:
    return len(text) >= 12 and bool(SQL_START.match(text))


def _joined_text(node) -> str:
    parts = []
    for v in node.values:
        if isinstance(v, ast.Constant):
            parts.append(str(v.value))
        elif isinstance(v, ast.FormattedValue):
            try:
                parts.append("{" + ast.unparse(v.value) + "}")
            except Exception:
                parts.append("{…}")
    return "".join(parts)


def find_sql(stmts, script_dir: Path):
    """SQL strings in these statements: [(text, start_line, file_or_None)], biggest first."""
    found = []
    for st in stmts:
        for x in ast.walk(st):
            text = None
            if isinstance(x, ast.Constant) and isinstance(x.value, str):
                text = x.value
                if text.lower().endswith(".sql") and "\n" not in text and len(text) < 200:
                    for base in (script_dir, Path.cwd()):
                        p = (base / text).resolve()
                        if p.is_file():
                            try:
                                body = p.read_text(encoding="utf-8", errors="replace")
                            except OSError:
                                continue
                            if is_sql_text(body):
                                found.append((body, 1, str(p)))
                            break
                    continue
            elif isinstance(x, ast.JoinedStr):
                text = _joined_text(x)
            if text and is_sql_text(text):
                found.append((text, x.lineno, None))
    found.sort(key=lambda t: -len(t[0]))
    return found


def sql_flow(nid: str, sql: dict, base_line: int, file: str | None) -> dict:
    """A query as a small pipeline: one block per CTE, one for the final statement, source tables as inputs."""
    nodes, edges = [], []
    off = (1 if file else base_line)
    base_tables = list(dict.fromkeys(r for c in sql["ctes"] + [sql["final"]] for r in c["reads"]))
    if base_tables:
        nodes.append({"id": "in", "kind": "input", "title": "tables", "lines": [off, off], "code": "", "reads": [],
                      "writes": base_tables, "refs": [], "role": "support", "sql_tables": True})
    by_name = {}
    for i, c in enumerate(sql["ctes"], 1):
        cid = f"c{i}"
        by_name[c["name"].upper()] = cid
        nodes.append({"id": cid, "kind": "code", "title": c["name"], "title_from": "code", "lang": "sql",
                      "lines": [off + c["lines"][0], off + c["lines"][1]], "code": c["code"],
                      "reads": c["reads"] + c["reads_ctes"], "writes": [c["name"]], "refs": [],
                      "role": "key" if c["reads_ctes"] or len(c["reads"]) > 1 else "support"})
        for t in c["reads"]:
            edges.append({"source": "in", "target": cid, "kind": "data", "vars": [t]})
        for t in c["reads_ctes"]:
            if t.upper() in by_name:
                edges.append({"source": by_name[t.upper()], "target": cid, "kind": "data", "vars": [t]})
    f = sql["final"]
    fid = "out"
    title = {"SELECT": "result", "INSERT": "insert into " + (", ".join(f["writes"]) or "…"), "CREATE": "create " + (", ".join(f["writes"]) or "…"),
             "MERGE": "merge into " + (", ".join(f["writes"]) or "…"), "UPDATE": "update " + (", ".join(f["writes"]) or "…"),
             "DELETE": "delete from " + (", ".join(f["writes"]) or "…")}.get(f["kind"], f["kind"].lower())
    nodes.append({"id": fid, "kind": "code", "title": title, "title_from": "code", "lang": "sql",
                  "lines": [off + f["lines"][0], off + f["lines"][1]], "code": f["code"],
                  "reads": f["reads"] + f["reads_ctes"], "writes": f["writes"], "refs": [], "returns": True, "role": "key"})
    for t in f["reads"]:
        edges.append({"source": "in", "target": fid, "kind": "data", "vars": [t]})
    for t in f["reads_ctes"]:
        if t.upper() in by_name:
            edges.append({"source": by_name[t.upper()], "target": fid, "kind": "data", "vars": [t]})
    for c in sql["ctes"]:   # a CTE nobody reads still feeds the result in practice (or is dead); link it so it isn't orphaned
        cid = by_name[c["name"].upper()]
        if not any(e["source"] == cid for e in edges):
            edges.append({"source": cid, "target": fid, "kind": "data", "vars": [c["name"]]})
    name = f"{nid}.sql"
    return {"name": name, "kind": "sql", "params": [], "config_params": [], "lines": [off, off + sql["lines"] - 1],
            "nodes": nodes, "edges": edges, "lookups": [], "file": file, "dialect": sql["dialect"]}


def sql_file_graph(path: str) -> dict:
    """A standalone .sql file: the map is its CTE flow."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    sql = parse_sql(text)
    flow = sql_flow("q", sql, 1, None)
    nodes = []
    for n in flow["nodes"]:
        m = dict(n)
        m["id"] = "n_" + n["id"]
        if n["kind"] == "input":
            m["kind"] = "value"; m["title"] = "source tables"; m["code"] = "\n".join("-- " + t for t in n["writes"])
        nodes.append(m)
    edges = [{"source": "n_" + e["source"], "target": "n_" + e["target"], "kind": e["kind"], "vars": e["vars"]} for e in flow["edges"]]
    doc = ""
    m = re.match(r"\s*((?:--[^\n]*\n)+)", text)
    if m:
        doc = " ".join(l.strip().lstrip("-").strip() for l in m.group(1).splitlines())
    return {
        "schema": SCHEMA_VERSION, "file": str(Path(path).resolve()), "file_name": Path(path).name,
        "source_hash": hashlib.sha1(text.encode()).hexdigest()[:12], "line_count": text.count("\n") + 1,
        "module_doc": doc[:600], "entry": None, "imports": [], "defs": {}, "sections": [], "lookups": [],
        "flows": {}, "nodes": nodes, "edges": edges, "notebook": None, "sql_file": True,
        "sql": {"dialect": sql["dialect"], "reads": sql["reads"], "writes": sql["writes"]},
        "tables": [{"name": t, "read_by": [n["id"] for n in nodes if t in n.get("reads", [])], "written_by": [n["id"] for n in nodes if t in n.get("writes", []) and n["kind"] != "value"]} for t in dict.fromkeys(sql["reads"] + sql["writes"])],
        "packages": [], "modules": [], "files": [str(Path(path).resolve())],
    }



# ---------- imports: the project's own modules, and third-party packages ----------
MAX_ADOPTED_DEFS = 40       # functions pulled in from local modules (direct imports + what they call)
ROOT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", ".git", "requirements.txt")


def _search_roots(script_dir: Path) -> list:
    roots, cur = [script_dir], script_dir
    for _ in range(6):
        if any((cur / m).exists() for m in ROOT_MARKERS):
            if cur not in roots:
                roots.append(cur)
            if (cur / "src").is_dir():
                roots.append(cur / "src")
            break
        if cur.parent == cur:
            break
        cur = cur.parent
        if cur not in roots:
            roots.append(cur)
    return roots


def _resolve_module(module: str, level: int, script_dir: Path) -> Path | None:
    """Path of a module imported by the script, if it lives in the project (not site-packages)."""
    if level:
        base = script_dir
        for _ in range(level - 1):
            base = base.parent
        bases = [base]
    else:
        bases = _search_roots(script_dir)
    parts = module.split(".") if module else []
    for b in bases:
        cand = b.joinpath(*parts) if parts else b
        for p in (cand.with_suffix(".py") if parts else None, cand / "__init__.py"):
            if p and p.is_file() and "site-packages" not in str(p) and "dist-packages" not in str(p):
                return p.resolve()
    return None


_pkg_cache: dict = {}
_dist_map: dict | None = None


def _dists():
    global _dist_map
    if _dist_map is None:
        try:
            import importlib.metadata as im
            _dist_map = im.packages_distributions()
        except Exception:
            _dist_map = {}
    return _dist_map


def package_meta(top: str) -> dict:
    """Version and one-line description of an installed distribution, without importing it."""
    if top in _pkg_cache:
        return _pkg_cache[top]
    info = {"name": top, "version": None, "summary": "", "stdlib": False}
    try:
        if top in getattr(sys, "stdlib_module_names", ()):
            info["stdlib"], info["summary"] = True, "Python standard library"
        else:
            import importlib.metadata as im
            dists = _dists().get(top) or [top]
            for dist in dists[:1]:
                md = im.metadata(dist)
                info["version"] = md.get("Version")
                info["summary"] = (md.get("Summary") or "")[:160]
                info["distribution"] = dist
    except Exception:
        pass
    _pkg_cache[top] = info
    return info


# --------------------------------------------------------------------------
# Graph builder
# --------------------------------------------------------------------------
class GraphBuilder:
    def __init__(self, source: str, path: str, notebook: dict | None = None):
        self.source = source
        self.notebook = notebook
        self.lines = source.splitlines()
        self.path = path
        self.tree = ast.parse(source, filename=path)
        self.nodes: list[dict] = []
        self.edges: dict[tuple, dict] = {}
        self.writers: dict[str, list] = {}
        self.pending: list[ast.stmt] = []
        self.imports: list[dict] = []
        self.imported: set[str] = set()
        self.defs: dict[str, dict] = {}
        self.module_doc = ast.get_docstring(self.tree) or ""
        self._counter = 0
        self.arms: list = []        # stack of {"of": branch node id, "label": arm label, "linked": bool}
        self.in_flow = False        # True while building a function's inner flow
        self.flow_name = None       # name of the function whose flow is being built
        self.sql_flows: dict = {}   # "<block>.sql" -> flow of that block's query (CTEs as blocks)
        self.import_map: dict = {}  # alias -> {"module": dotted module, "name": member or None, "level": n}
        self.modules: list = []     # local modules the script imports from, with what it pulled in
        self.ext_flows: dict = {}   # flows of functions adopted from local modules
        self.packages: dict = {}    # top-level package -> {"modules": set, "aliases": set}
        self.adopted = 0
        self.entry = None
        self.section_heads = []
        self.module_names = set()
        self.consts = {}

    # ---------- helpers ----------
    def seg(self, start, end):
        return "\n".join(self.lines[start - 1:end])

    def new_id(self, prefix):
        self._counter += 1
        return f"{prefix}{self._counter}"

    def leading_comment(self, lineno):
        """Comment lines directly above `lineno` (no blank line between)."""
        out, i = [], lineno - 2
        while i >= 0 and self.lines[i].strip().startswith("#") and not self.lines[i].strip().startswith("#>"):
            out.append(self.lines[i].strip().lstrip("#").strip(" -=*"))
            i -= 1
        out.reverse()
        text = " ".join(s for s in out if s)
        return text, (i + 2 if out else lineno)

    def stmt_start(self, st):
        decos = getattr(st, "decorator_list", None)
        if decos:
            return min(d.lineno for d in decos)
        return st.lineno

    def first_line(self, st, limit=70):
        line = self.lines[st.lineno - 1].strip()
        if st.end_lineno and st.end_lineno > st.lineno:
            line += " …"
        return line if len(line) <= limit else line[: limit - 1] + "…"

    def call_key(self, node):
        """The defs key a call resolves to: `f(...)` or `mod.f(...)` for a module imported from the project."""
        if not isinstance(node, ast.Call):
            return None
        f = node.func
        if isinstance(f, ast.Name) and f.id in self.defs:
            return f.id
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f"{f.value.id}.{f.attr}" in self.defs:
            return f"{f.value.id}.{f.attr}"
        return None

    def is_local_call(self, node):
        return self.call_key(node) is not None

    def has_local_call(self, node):
        return any(self.is_local_call(n) for n in ast.walk(node))

    def add_edge(self, src, dst, var, kind="data"):
        if not src or src == dst:
            return
        key = (src, dst, kind)
        e = self.edges.get(key)
        if not e:
            e = self.edges[key] = {"source": src, "target": dst, "kind": kind, "vars": []}
        if var and var not in e["vars"]:
            e["vars"].append(var)

    def register(self, node):
        """Add a node; inside a branch arm, link the arm's first block to the branch node."""
        self.nodes.append(node)
        if self.arms:
            arm = self.arms[-1]
            node["arm"] = {"of": arm["of"], "label": arm["label"]}
            if not arm["linked"]:
                self.add_edge(arm["of"], node["id"], arm["label"], "branch")
                arm["linked"] = True

    def link_reads(self, node_id, names, kind="data"):
        for n in names:
            for w in self.writers.get(n, ()):
                self.add_edge(w, node_id, n, kind)

    def set_writes(self, node_id, names):
        for n in names:
            self.writers[n] = [node_id]

    # ---------- pass 1: definitions ----------
    def collect_defs(self):
        module_names = set()
        for st in self.tree.body:
            if isinstance(st, (ast.Import, ast.ImportFrom)):
                rec = {"line": st.lineno, "code": self.seg(st.lineno, st.end_lineno), "names": []}
                for a in st.names:
                    alias = (a.asname or a.name).split(".")[0]
                    self.imported.add(alias)
                    if isinstance(st, ast.ImportFrom):
                        self.import_map[a.asname or a.name] = {"module": st.module or "", "name": a.name, "level": st.level}
                        rec["module"] = ("." * st.level) + (st.module or "")
                    else:
                        self.import_map[alias] = {"module": a.name if a.asname else a.name.split(".")[0], "name": None, "level": 0,
                                                  "full": a.name}
                        rec["module"] = a.name
                    rec["names"].append(a.asname or a.name)
                self.imports.append(rec)
            elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.defs[st.name] = {"node": st}
            else:
                module_names.update(collect(st).writes)
        self.module_names = module_names - set(self.defs) - self.imported
        # Module-level string constants, so `{PROJECT}.crm.contacts` in an f-string query can be read as a real table name.
        self.consts = {}
        for st in self.tree.body:
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                if isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
                    self.consts[st.targets[0].id] = st.value.value
                elif isinstance(st.value, ast.JoinedStr):
                    self.consts[st.targets[0].id] = _joined_text(st.value)
        for _ in range(2):
            self.consts = {k: re.sub(r"\{(\w+)\}", lambda m: self.consts.get(m.group(1), m.group(0)), v) for k, v in self.consts.items()}

        for name, d in self.defs.items():
            st = d["node"]
            start = self.stmt_start(st)
            is_class = isinstance(st, ast.ClassDef)
            params = [] if is_class else _arg_names(st.args)
            param_types = {} if is_class else _arg_types(st.args)
            if is_class:
                init = next(
                    (b for b in st.body if isinstance(b, ast.FunctionDef) and b.name == "__init__"),
                    None,
                )
                params = [p for p in _arg_names(init.args) if p != "self"] if init else []

            loads, stores, global_decl = {}, set(), set()
            for sub in ast.walk(st):
                if isinstance(sub, ast.Name):
                    if isinstance(sub.ctx, ast.Load):
                        loads.setdefault(sub.id)
                    else:
                        stores.add(sub.id)
                elif isinstance(sub, ast.AugAssign) and isinstance(sub.target, ast.Name):
                    loads.setdefault(sub.target.id)
                elif isinstance(sub, (ast.Global, ast.Nonlocal)):
                    global_decl.update(sub.names)
                elif isinstance(sub, ast.arg):
                    stores.add(sub.arg)

            refs = [n for n in loads if n in self.defs and n != name]
            global_reads = [
                n for n in loads
                if n in self.module_names and (n not in stores or n in global_decl)
            ]
            global_writes = [n for n in global_decl if n in stores and n in self.module_names]
            doc = ast.get_docstring(st) or ""
            d["packages"] = self.packages_in([st])
            d.update(
                name=name,
                kind="class" if is_class else ("async" if isinstance(st, ast.AsyncFunctionDef) else "function"),
                lines=[start, st.end_lineno],
                code=self.seg(start, st.end_lineno),
                params=params,
                param_types=param_types,
                doc=doc.strip().splitlines()[0] if doc.strip() else "",
                refs=refs,
                global_reads=global_reads,
                global_writes=global_writes,
                used_at_top=False,
            )

    # ---------- sections from comment banners ----------
    _RULE = re.compile(r"^#\s*([-=*#~_])\1{3,}\s*$")
    _INLINE = re.compile(r"^#\s*[-=*#~]{2,}\s*(\S.*?)\s*[-=*#~]{2,}\s*$")
    _CELL = re.compile(r"^#\s*%%\s*(.*)$")

    def find_section_heads(self):
        """Top-level banner comments that mark sections of the script:
            # -----------        # --- Title ---        # %% Title
            # Title
            # -----------
        """
        inside = set()  # lines inside multi-line strings (docstrings, HTML templates)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.end_lineno and node.end_lineno > node.lineno:
                inside.update(range(node.lineno + 1, node.end_lineno + 1))
        heads, i, n = [], 0, len(self.lines)
        while i < n:
            ln, line = i + 1, self.lines[i].rstrip()
            if ln in inside or not line.startswith("#"):
                i += 1
                continue
            m = self._CELL.match(line)
            if m:
                heads.append((ln, m.group(1).strip() or f"Cell at line {ln}"))
                i += 1
                continue
            if self._RULE.match(line):
                j, texts = i + 1, []
                while j < n and self.lines[j].startswith("#") and not self._RULE.match(self.lines[j].rstrip()):
                    t = self.lines[j].lstrip("#").strip()
                    if t:
                        texts.append(t)
                    j += 1
                if texts:
                    heads.append((ln, texts[0]))
                    closing = j < n and self._RULE.match(self.lines[j].rstrip())
                    i = j + 1 if closing else j
                    continue
                i += 1
                continue
            m = self._INLINE.match(line)
            if m:
                heads.append((ln, m.group(1).strip()))
            i += 1
        return heads

    @staticmethod
    def _clean_title(t, limit=56):
        t = t.strip().rstrip(":").strip()
        if len(t) <= limit:
            return t
        cut = t[:limit].rsplit(" ", 1)[0]
        return (cut if len(cut) > 20 else t[:limit]) + "…"

    def build_sections(self):
        heads = self.section_heads
        if not heads or not self.nodes:
            return []
        bounds = []
        first = min(nd["lines"][0] for nd in self.nodes)
        if first < heads[0][0]:
            bounds.append((1, "Setup"))
        bounds += heads
        secs = []
        for k, (start, title) in enumerate(bounds):
            stop = bounds[k + 1][0] - 1 if k + 1 < len(bounds) else len(self.lines)
            secs.append({"title": self._clean_title(title), "lines": [start, stop], "nodes": []})
        placed = {}
        for nd in self.nodes:
            if nd.get("file"):
                continue   # a block from another file: placed with its first user below
            ln = nd["lines"][0]
            target = secs[0]
            for sec in secs:
                if ln >= sec["lines"][0]:
                    target = sec
            target["nodes"].append(nd["id"])
            placed[nd["id"]] = target
        for nd in self.nodes:
            if not nd.get("file"):
                continue
            user = next((u for u in self.nodes if nd.get("def") and nd["def"] in u.get("refs", []) and u["id"] in placed), None)
            target = placed[user["id"]] if user else secs[-1]
            target["nodes"].append(nd["id"])
        secs = [sec for sec in secs if sec["nodes"]]
        if len(secs) < 2:
            return []
        for i, sec in enumerate(secs, 1):
            sec["id"] = f"s{i}"
        return secs

    def packages_in(self, stmts) -> list:
        """Third-party / stdlib packages a piece of code touches, by the import aliases it loads."""
        out = []
        for st in stmts:
            for x in ast.walk(st):
                if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in self.import_map and x.id in self.imported:
                    rec = self.import_map[x.id]
                    if rec.get("local"):
                        continue
                    top = (rec["module"] or rec["name"] or x.id).lstrip(".").split(".")[0]
                    if top and top not in out:
                        out.append(top)
        return out

    def resolve_local_imports(self):
        """Pull functions imported from the project's own modules into this graph (one level deep),
        so a call to them is a real step block with code and an openable flow, not an opaque line."""
        script_dir = Path(self.path).resolve().parent
        builders: dict = {}

        def builder_for(path: Path):
            if path not in builders:
                try:
                    src = path.read_text(encoding="utf-8")
                    mb = GraphBuilder(src, str(path))
                    mb.collect_defs()
                    builders[path] = mb
                except (OSError, SyntaxError, RecursionError):
                    builders[path] = None
            return builders[path]

        def adopt(mb, local, key, modname, path, record):
            """Copy a module function into self.defs under `key`, and its module-local callees after it."""
            if key in self.defs:
                return
            if self.adopted >= MAX_ADOPTED_DEFS:
                return
            d = mb.defs.get(local)
            if not d:
                return
            self.adopted += 1
            short = modname.split(".")[-1] or path.stem
            keyfor = lambda r: record["keys"].get(r) or f"{short}.{r}"
            nd = {k: v for k, v in d.items() if k != "node"}
            nd["node"] = d["node"]
            nd["name"] = key
            nd["refs"] = [keyfor(r) for r in d["refs"]]
            nd["origin"] = {"file": str(path), "module": modname, "name": local}
            nd["used_at_top"] = False
            self.defs[key] = nd
            record["adopted"].append(key)
            for r in d["refs"]:
                adopt(mb, r, keyfor(r), modname, path, record)
            if d["kind"] in ("function", "async"):
                try:
                    f = mb.build_flow(d)
                except Exception:
                    f = None
                if f:
                    for n in f["nodes"]:
                        if n.get("def"):
                            n["def"] = keyfor(n["def"])
                        n["refs"] = [keyfor(r) if r in mb.defs else r for r in n.get("refs", [])]
                        n["file"] = str(path)
                    f["name"], f["file"] = key, str(path)
                    for sk, sf in list(mb.sql_flows.items()):
                        if sk.startswith(local + "/"):
                            nk = key + sk[len(local):]
                            for n in sf["nodes"]:
                                n["file"] = sf.get("file") or str(path)
                            self.ext_flows[nk] = sf
                            for n in f["nodes"]:
                                if n.get("sql_flow") == sk:
                                    n["sql_flow"] = nk
                    self.ext_flows[key] = f

        def record_for(modname, path, mb):
            rec_ = next((m for m in self.modules if m["file"] == str(path)), None)
            if not rec_:
                rec_ = {"module": modname, "file": str(path), "imported": [], "adopted": [], "keys": {},
                        "doc": mb.module_doc.strip().splitlines()[0] if mb.module_doc.strip() else ""}
                self.modules.append(rec_)
            return rec_

        def adopt_attrs(alias, mb, modname, path, record):
            """`alias.f(...)` calls on a module object: adopt the functions the script uses."""
            used = {x.attr for x in ast.walk(self.tree) if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == alias}
            for attr in sorted(used):
                if attr in mb.defs:
                    record["keys"][attr] = f"{alias}.{attr}"
            for attr in sorted(used):
                if attr in mb.defs:
                    adopt(mb, attr, f"{alias}.{attr}", modname, path, record)
                    record["imported"].append(f"{alias}.{attr}")
                elif attr in mb.consts:
                    self.consts.setdefault(f"{alias}.{attr}", mb.consts[attr])

        for alias, rec in list(self.import_map.items()):
            if rec["name"] is None:
                # `import pkg.mod [as m]`
                path = _resolve_module(rec.get("full") or rec["module"], 0, script_dir)
                if not path:
                    continue
                mb = builder_for(path)
                if not mb:
                    continue
                rec["local"] = True
                modname = (rec.get("full") or rec["module"]).lstrip(".")
                adopt_attrs(alias, mb, modname, path, record_for(modname, path, mb))
                continue
            # `from pkg.mod import name`: name may be a function, a constant, or a submodule
            name = rec["name"]
            sub = _resolve_module(f"{rec['module']}.{name}" if rec["module"] else name, rec["level"], script_dir)
            if sub:
                smb = builder_for(sub)
                if not smb:
                    continue
                rec["local"] = True
                modname = (f"{rec['module']}.{name}" if rec["module"] else name).lstrip(".")
                adopt_attrs(alias, smb, modname, sub, record_for(modname, sub, smb))
                continue
            path = _resolve_module(rec["module"], rec["level"], script_dir)
            if not path:
                continue
            mb = builder_for(path)
            if not mb:
                continue
            rec["local"] = True
            modname = (rec["module"] or path.stem).lstrip(".")
            record = record_for(modname, path, mb)
            if name in mb.defs:
                record["keys"][name] = alias
                adopt(mb, name, alias, modname, path, record)
                record["imported"].append(alias)
                self.imported.discard(alias)
            elif name in mb.consts:
                self.consts.setdefault(alias, mb.consts[name])
                record["imported"].append(alias)
        self.module_names -= set(self.defs)
        for m in self.modules:
            m.pop("keys", None)

    def package_table(self):
        used_by = {}
        for nd in self.nodes:
            for pkg in nd.get("packages", []):
                used_by.setdefault(pkg, []).append(nd["id"])
        tops = []
        for alias, rec in self.import_map.items():
            if rec.get("local"):
                continue
            top = (rec["module"] or rec["name"] or alias).lstrip(".").split(".")[0]
            if top and top not in tops:
                tops.append(top)
        out = []
        for top in tops:
            info = dict(package_meta(top))
            info["used_by"] = used_by.get(top, [])
            info["imports"] = [rec["code"] for rec in self.imports if any((n.split(".")[0] in self.import_map and (self.import_map[n.split(".")[0]]["module"] or self.import_map[n.split(".")[0]]["name"] or "").lstrip(".").split(".")[0] == top) for n in rec["names"])]
            out.append(info)
        return out

    # ---------- pass 2: the pipeline ----------
    def flush(self):
        if not self.pending:
            return
        stmts, self.pending = self.pending, []
        groups, cur = [], [stmts[0]]
        for prev, st in zip(stmts, stmts[1:]):
            gap = self.lines[prev.end_lineno:st.lineno - 1]
            blank_gap = any(not l.strip() for l in gap)
            new_section = any(prev.end_lineno < h <= st.lineno for h, _ in self.section_heads)
            if blank_gap or new_section:
                groups.append(cur)
                cur = [st]
            else:
                cur.append(st)
        groups.append(cur)
        for g in groups:
            self.make_code_node(g)

    _VALUE_NODES = (
        ast.Constant, ast.Name, ast.Load, ast.BinOp, ast.UnaryOp, ast.operator, ast.unaryop,
        ast.JoinedStr, ast.FormattedValue, ast.List, ast.Tuple, ast.Dict, ast.Set,
        ast.Attribute, ast.Call, ast.keyword,
    )
    _VALUE_CALLS = ("Path", "dict", "list", "tuple", "set", "int", "float", "str", "frozenset")

    def _is_value(self, st):
        """A setting: NAME = <literal-ish expression>."""
        if not isinstance(st, (ast.Assign, ast.AnnAssign)) or st.value is None:
            return False
        targets = st.targets if isinstance(st, ast.Assign) else [st.target]
        if not all(isinstance(t, ast.Name) for t in targets):
            return False
        for n in ast.walk(st.value):
            if not isinstance(n, self._VALUE_NODES):
                return False
            if isinstance(n, ast.Call) and not (
                isinstance(n.func, ast.Name) and n.func.id in self._VALUE_CALLS
            ):
                return False
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and (
                n.id in self.defs
                or (n.id not in self.module_names and n.id not in self._VALUE_CALLS)
            ):
                return False
        return True

    def attach_sql(self, node, stmts):
        """Find SQL in a block's strings; record tables read/written and build the query's own flow."""
        found = find_sql(stmts, Path(self.path).resolve().parent)
        if not found:
            return
        reads, writes, primary = [], [], None
        consts = getattr(self, "consts", {})
        for text, line, file in found:
            text = re.sub(r"\{(\w+)\}", lambda m: consts.get(m.group(1), m.group(0)), text)
            parsed = parse_sql(text)
            if primary is None:
                primary = (parsed, line, file)
            reads += [r for r in parsed["reads"] if r not in reads]
            writes += [w for w in parsed["writes"] if w not in writes]
        parsed, line, file = primary
        node["sql"] = {"dialect": parsed["dialect"], "kind": parsed["kind"], "reads": reads, "writes": writes,
                       "file": file, "queries": len(found)}
        m = re.search(r"#> (%%\w+)(?:\s+(\w+))?", (node.get("code") or "").split("\n", 1)[0])
        if m:   # a notebook SQL cell: title it by its magic and result variable
            node["title"], node["title_from"] = m.group(1) + (f" → {node['writes'][0]}" if node.get("writes") else ""), "code"
            node["kind"] = "code"
        if parsed["ctes"] or len(parsed["reads"]) >= 2 or parsed["writes"]:
            key = (f"{self.flow_name}/" if self.in_flow and self.flow_name else "") + f"{node['id']}.sql"
            self.sql_flows[key] = sql_flow(node["id"], parsed, line, file)
            node["sql_flow"] = key

    def link_tables(self):
        """Block A writes table T, a later block B reads T: a 'table' edge, the SQL counterpart of a variable."""
        written = {}
        for nd in self.nodes:
            sq = nd.get("sql")
            if not sq:
                continue
            for t in sq["reads"]:
                for w in written.get(t.lower(), ()):
                    self.add_edge(w, nd["id"], t, "table")
            for t in sq["writes"]:
                written.setdefault(t.lower(), []).append(nd["id"])

    def make_code_node(self, stmts):
        """Plain top-level code. Loops, ifs and `with` blocks stay whole."""
        nid = self.new_id("n")
        comment, top = self.leading_comment(stmts[0].lineno)
        reads, writes, written = {}, {}, set()
        for st in stmts:
            r, w = stmt_rw(st, written)
            for n in r:
                reads.setdefault(n)
            for n in w:
                writes.setdefault(n)
        is_value = all(self._is_value(s) for s in stmts)
        out_names = [w for w in writes if w not in self.defs]
        if comment:
            title, title_from = comment, "comment"
        elif is_value and len(stmts) > 1:
            title, title_from = "Settings", "comment"
        elif len(stmts) == 1:
            title, title_from = self.first_line(stmts[0]), "code"
        elif out_names:
            more = len(out_names) - 3
            title, title_from = "→ " + ", ".join(out_names[:3]) + (f" +{more}" if more > 0 else ""), "code"
        else:
            first = self.first_line(stmts[0], 60)
            title, title_from = (first if first.endswith("…") else first + " …"), "code"
        # Local functions used anywhere inside (also inside loops / f-strings).
        refs = list(dict.fromkeys(
            n.id for st in stmts for n in ast.walk(st)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in self.defs
        ))
        for r in refs:
            self.defs[r]["used_inside_code"] = True
        node = {
            "id": nid,
            "kind": "value" if is_value else "code",
            "title": self._clean_title(title, 70) if title_from == "comment" else title,
            "title_from": title_from,
            "lines": [top, stmts[-1].end_lineno],
            "code": self.seg(top, stmts[-1].end_lineno),
            "reads": [r for r in reads if self._trackable(r)],
            "writes": [w for w in writes if w not in self.defs and w not in self.imported],
            "refs": refs,
        }
        if any(isinstance(x, ast.Return) for st in stmts for x in ast.walk(st)):
            node["returns"] = True
        node["_side_only"] = all(_side_stmt(st) for st in stmts)
        node["_writes_file"] = _writes_file(stmts)
        node["packages"] = self.packages_in(stmts)
        self.attach_sql(node, stmts)
        self.register(node)
        self.link_reads(nid, node["reads"])
        self.set_writes(nid, node["writes"])
        return nid

    def _trackable(self, name):
        return name not in BUILTIN_NAMES and name not in self.imported and name not in self.defs

    def direct_call(self, st):
        """`f(...)`, `x = f(...)` or `a, b = f(...)` where f is defined in this file."""
        if not isinstance(st, (ast.Expr, ast.Assign, ast.AnnAssign, ast.Return)):
            return None
        v = st.value
        if isinstance(v, ast.Await):
            v = v.value
        return v if v is not None and self.is_local_call(v) else None

    def make_step_node(self, st, call):
        """A pipeline step: one statement whose value is a call to a local function."""
        nid = self.new_id("n")
        d = self.defs[self.call_key(call)]
        if not self.in_flow:
            d["used_at_top"] = True
        c = collect(st)
        reads = [r for r in c.reads if self._trackable(r)]
        writes = [w for w in c.writes if w not in self.defs]
        writes += [w for w in d["global_writes"] if w not in writes]
        nested = [n.id for n in ast.walk(call) if isinstance(n, ast.Name)
                  and isinstance(n.ctx, ast.Load) and n.id in self.defs and n.id != d["name"]]
        for r in nested:
            self.defs[r]["used_inside_code"] = True
        comment, _ = self.leading_comment(st.lineno)
        node = {
            "id": nid,
            "kind": "step",
            "title": d["name"],
            "def": d["name"],
            "lines": [st.lineno, st.end_lineno],
            "call": self.seg(st.lineno, st.end_lineno),
            "note": comment,
            "reads": reads,
            "global_reads": list(d["global_reads"]),
            "writes": writes,
            "refs": list(dict.fromkeys(nested + d["refs"])),
            "packages": list(dict.fromkeys(self.packages_in([st]) + d.get("packages", []))),
        }
        if isinstance(st, ast.Return) or any(isinstance(x, ast.Return) for x in ast.walk(st)):
            node["returns"] = True
        self.attach_sql(node, [st])
        self.register(node)
        self.link_reads(nid, reads)
        self.link_reads(nid, d["global_reads"], kind="global")
        self.set_writes(nid, writes)

    # ---------- branches: an if/match whose arms call local functions ----------
    def branch_arms(self, st):
        """Arms of an if/elif/else chain or a match, as (label, body) pairs.
        Returns None unless at least one arm contains a call to a local function."""
        arms = []
        if isinstance(st, ast.If):
            cur, first = st, True
            while True:
                head = self.lines[cur.lineno - 1].strip()
                label = head if first else ("elif " + head[3:] if head.startswith("if ") else head)
                arms.append((label.rstrip(":"), cur.body))
                first = False
                if len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If) and \
                        self.lines[cur.orelse[0].lineno - 1].strip().startswith("elif"):
                    cur = cur.orelse[0]
                    continue
                if cur.orelse:
                    arms.append(("else", cur.orelse))
                break
        elif hasattr(ast, "Match") and isinstance(st, ast.Match):
            for c in st.cases:
                arms.append((self.lines[c.pattern.lineno - 1].strip().rstrip(":"), c.body))
        else:
            return None
        if not any(self.has_local_call(x) for _, body in arms for x in body):
            return None
        return arms

    def make_branch_node(self, st, arms):
        nid = self.new_id("b")
        test = st.test if isinstance(st, ast.If) else st.subject
        c = collect(test)
        header = self.lines[st.lineno - 1].strip().rstrip(":")
        comment, _ = self.leading_comment(st.lineno)
        node = {
            "id": nid,
            "kind": "branch",
            "title": header if len(header) <= 80 else header[:79] + "…",
            "title_from": "code",
            "lines": [st.lineno, st.end_lineno],
            "code": self.lines[st.lineno - 1],
            "note": comment,
            "arms": [label for label, _ in arms],
            "reads": [r for r in c.reads if self._trackable(r)],
            "writes": [],
            "refs": [],
        }
        self.register(node)
        self.link_reads(nid, node["reads"])
        return nid

    def run_branches(self, bid, arms, may_skip):
        """Walk each arm from the same starting state, then merge: afterwards a
        variable may come from any arm (reaching definitions)."""
        start = {k: list(v) for k, v in self.writers.items()}
        results = [start] if may_skip else []
        for label, body in arms:
            self.writers = {k: list(v) for k, v in start.items()}
            self.arms.append({"of": bid, "label": label, "linked": False})
            self.walk(body)
            self.flush()
            self.arms.pop()
            results.append(self.writers)
        merged = {}
        for res in results:
            for k, v in res.items():
                lst = merged.setdefault(k, [])
                for x in v:
                    if x not in lst:
                        lst.append(x)
        # Accumulators (`parts.append(...)` in several arms): if one writer already
        # feeds another writer of the same variable, keep only the downstream one.
        for var, lst in merged.items():
            if len(lst) > 1:
                fed = {src for (src, dst, kind), e in self.edges.items()
                       if kind == "data" and var in e["vars"] and src in lst and dst in lst}
                merged[var] = [w for w in lst if w not in fed] or lst
        self.writers = merged

    ENTRY_NAMES = ("main", "run", "cli", "entrypoint", "entry_point", "pipeline")

    def entry_call(self, st):
        """If `st` is `main()`, `x = main()`, `sys.exit(main())` or `raise SystemExit(main())`
        where main is defined in this file and called nowhere else, return that function's
        def. Its body is then read as the pipeline instead of showing one opaque block."""
        if isinstance(st, ast.Raise):
            v = st.exc
        elif isinstance(st, (ast.Expr, ast.Assign)):
            v = st.value
        else:
            return None
        # Unwrap sys.exit(...) / exit(...) / SystemExit(...)
        if isinstance(v, ast.Call) and len(v.args) == 1 and not v.keywords:
            f = v.func
            fname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
            if fname in ("exit", "SystemExit", "_exit") and not self.is_local_call(v):
                v = v.args[0]
        if not self.is_local_call(v) or v.args or v.keywords:
            return None
        name = v.func.id
        d = self.defs[name]
        if d["kind"] != "function" or d.get("params"):
            return None
        calls = sum(
            1 for n in ast.walk(self.tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
        )
        if calls != 1:
            return None
        only_call = sum(1 for n in ast.walk(self.tree) if self.is_local_call(n) and n is not v) == 0
        return d if (name in self.ENTRY_NAMES or only_call) else None

    def is_main_guard(self, st):
        if not isinstance(st, ast.If):
            return False
        t = st.test
        return (
            isinstance(t, ast.Compare)
            and isinstance(t.left, ast.Name)
            and t.left.id == "__name__"
            and len(t.comparators) == 1
            and isinstance(t.comparators[0], ast.Constant)
            and t.comparators[0].value == "__main__"
        )

    def walk(self, stmts, top_level=False):
        for i, st in enumerate(stmts):
            if top_level and i == 0 and self.module_doc and isinstance(st, ast.Expr):
                continue
            if isinstance(st, (ast.Import, ast.ImportFrom)):
                if not top_level:
                    self.pending.append(st)
                continue
            if top_level and isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if self.is_main_guard(st):
                self.flush()
                self.walk(st.body)
                self.flush()
                continue
            arms = None if self.is_main_guard(st) else self.branch_arms(st)
            if arms is not None:
                self.flush()
                bid = self.make_branch_node(st, arms)
                has_else = any(label == "else" for label, _ in arms) or (
                    hasattr(ast, "Match") and isinstance(st, ast.Match) and any(l.strip() in ("case _", "case _:") for l, _ in arms))
                self.run_branches(bid, arms, may_skip=not has_else)
                continue
            entry = self.entry_call(st) if (self.entry is None and not self.in_flow) else None
            if entry is not None:
                self.flush()
                self.entry = {"name": entry["name"], "lines": entry["lines"], "call_line": st.lineno}
                entry["used_at_top"] = True
                self.walk(entry["node"].body)
                self.flush()
                continue
            call = self.direct_call(st)
            if call is not None:
                self.flush()
                self.make_step_node(st, call)
                continue
            self.pending.append(st)
        self.flush()

    # ---------- pass 3: helpers ----------
    def add_helpers(self):
        helper_ids = {}
        for name, d in self.defs.items():
            if d["used_at_top"]:
                continue
            nid = self.new_id("h")
            helper_ids[name] = nid
            self.nodes.append({
                "id": nid,
                "kind": "helper",
                "title": name,
                "def": name,
                "lines": d["lines"],
                "reads": [],
                "global_reads": list(d["global_reads"]),
                "writes": [],
                "refs": list(d["refs"]),
                "packages": list(d.get("packages", [])),
                **({"file": d["origin"]["file"]} if d.get("origin") else {}),
            })
        used = set()
        for node in self.nodes:
            for ref in node.get("refs", []):
                if ref in helper_ids:
                    self.add_edge(helper_ids[ref], node["id"], f"{ref}()", "calls")
                    used.add(ref)
        for name, nid in helper_ids.items():
            if name not in used:
                next(n for n in self.nodes if n["id"] == nid)["kind"] = "unused"
            for g in self.defs[name]["global_reads"]:
                for w in self.writers.get(g, ()):
                    self.add_edge(w, nid, g, "global")

    # ---------- function flows (opened on demand in the viewer) ----------
    def build_flow(self, d):
        """Read a function's body as its own small pipeline. Parameters come
        from an 'inputs' block; blocks that `return` are marked."""
        st = d["node"]
        body = st.body[1:] if (st.body and isinstance(st.body[0], ast.Expr)
                               and isinstance(st.body[0].value, ast.Constant)
                               and isinstance(st.body[0].value.value, str)) else st.body
        if len(body) < 2 and not any(self.has_local_call(x) for x in body):
            return None
        saved = (self.nodes, self.edges, self.writers, self.pending, self._counter, self.arms, self.in_flow)
        self.nodes, self.edges, self.writers, self.pending, self._counter, self.arms = [], {}, {}, [], 0, []
        self.in_flow = True
        self.flow_name = d["name"]
        try:
            params = [p for p in d["params"] if p not in ("self", "cls")]
            config = {p for p in params if _is_config_type(d.get("param_types", {}).get(p))}
            if params:
                node = {"id": "in", "kind": "input", "title": "inputs", "lines": [st.lineno, st.lineno],
                        "code": "", "reads": [], "writes": list(params), "refs": []}
                self.nodes.append(node)
                self.set_writes("in", params)
            self.walk(body)
            self.flush()
            self.link_tables()
            self.mark_lookups(config_params=config)
            nodes, edges, lookups = self.nodes, list(self.edges.values()), self.lookups
            assign_roles(nodes, edges)
        finally:
            (self.nodes, self.edges, self.writers, self.pending, self._counter, self.arms, self.in_flow) = saved
            self.flow_name = None
        real = [n for n in nodes if n["kind"] != "input"]
        if len(real) < 2:
            return None
        return {"name": d["name"], "params": params, "config_params": sorted(config), "lines": d["lines"],
                "nodes": nodes, "edges": edges, "lookups": lookups}

    def build_flows(self):
        flows = {}
        for name, d in self.defs.items():
            if self.entry and name == self.entry["name"]:
                continue  # its body is already the top-level pipeline
            if d.get("origin"):
                continue  # built by the module's own builder (ext_flows)
            if d["kind"] in ("function", "async"):
                f = self.build_flow(d)
                if f:
                    flows[name] = f
        flows.update(self.ext_flows)
        return flows

    # ---------- pass 4: lookups ----------
    def mark_lookups(self, config_params=()):
        """Values read almost everywhere (constants like CAT_META, helpers like
        fmt_int) would draw an arrow to every block. Mark those links as
        "lookup" so the viewer can show them as tags instead of arrows.
        Rule (deliberately conservative): written by exactly one block, never
        changed afterwards, read by 3+ other blocks, and either an UPPER_CASE
        name or a literal setting. Helpers count when 3+ blocks use them."""
        writers, readers = {}, {}
        for n in self.nodes:
            for w in n.get("writes", []):
                writers.setdefault(w, set()).add(n["id"])
            for r in n.get("reads", []) + n.get("global_reads", []):
                readers.setdefault(r, set()).add(n["id"])
        by_id = {n["id"]: n for n in self.nodes}
        lookup_vars = {}
        for var, ws in writers.items():
            if len(ws) != 1:
                continue
            w = next(iter(ws))
            rs = readers.get(var, set()) - {w}
            is_setting = var.isupper() or by_id[w]["kind"] == "value"
            is_param = by_id[w]["kind"] == "input"
            if (len(rs) >= 3 and is_setting) or (is_param and (len(rs) >= 4 or (len(rs) >= 2 and var in config_params))):
                lookup_vars[var] = (w, len(rs))
        callers = {}
        for (src, dst, kind) in self.edges:
            if kind == "calls":
                callers.setdefault(src, set()).add(dst)
        hub_helpers = {src for src, us in callers.items() if len(us) >= 3}

        old, self.edges = self.edges, {}
        for (src, dst, kind), e in old.items():
            for v in e["vars"]:
                k = "lookup" if (v in lookup_vars or (kind == "calls" and src in hub_helpers)) else kind
                self.add_edge(src, dst, v, k)
        for n in self.nodes:
            lk = [v for v in n.get("reads", []) + n.get("global_reads", []) if v in lookup_vars]
            n["reads"] = [v for v in n.get("reads", []) if v not in lookup_vars]
            n["global_reads"] = [v for v in n.get("global_reads", []) if v not in lookup_vars]
            n["lookups"] = list(dict.fromkeys(lk))
        self.lookups = [
            {"name": v, "node": w, "line": by_id[w]["lines"][0], "used_by": cnt, "kind": "value"}
            for v, (w, cnt) in sorted(lookup_vars.items(), key=lambda kv: by_id[kv[1][0]]["lines"][0])
        ] + [
            {"name": by_id[h]["def"] + "()", "node": h, "line": by_id[h]["lines"][0], "used_by": len(callers[h]), "kind": "helper"}
            for h in sorted(hub_helpers, key=lambda h: by_id[h]["lines"][0])
        ]

    def retitle(self):
        """Untitled multi-statement blocks are named after what they produce.
        Prefer names that later blocks actually use over loop temporaries."""
        used = {}
        for (src, _dst, kind), e in self.edges.items():
            if kind != "calls":
                lst = used.setdefault(src, [])
                lst += [v for v in e["vars"] if v not in lst]
        for n in self.nodes:
            if n.get("title_from") == "code" and n["title"].startswith("→ "):
                names = [w for w in n["writes"] if w in used.get(n["id"], [])] or n["writes"]
                more = len(names) - 3
                n["title"] = "→ " + ", ".join(names[:3]) + (f" +{more}" if more > 0 else "")

    # ---------- run ----------
    def build(self):
        self.entry = None
        self.collect_defs()
        self.resolve_local_imports()
        self.section_heads = self.find_section_heads()
        self.walk(self.tree.body, top_level=True)
        self.add_helpers()
        self.mark_lookups()
        self.retitle()
        self.link_tables()
        notebook = self.notebook_info() if self.notebook else None   # may add forward-reference edges
        assign_roles(self.nodes, list(self.edges.values()))
        flows = self.build_flows()
        flows.update(self.sql_flows)
        tables = {}
        for nd in self.nodes:
            for t in (nd.get("sql") or {}).get("reads", []):
                tables.setdefault(t, {"name": t, "read_by": [], "written_by": []})["read_by"].append(nd["id"])
            for t in (nd.get("sql") or {}).get("writes", []):
                tables.setdefault(t, {"name": t, "read_by": [], "written_by": []})["written_by"].append(nd["id"])
        defs_out = {
            name: {k: v for k, v in d.items() if k not in ("node", "used_at_top", "used_inside_code")}
            for name, d in self.defs.items()
        }
        files = [str(Path(self.path).resolve())] + [m["file"] for m in self.modules]
        h = hashlib.sha1(self.source.encode())
        for m in self.modules:
            try:
                h.update(Path(m["file"]).read_bytes())
            except OSError:
                pass
        return {
            "schema": SCHEMA_VERSION,
            "file": str(Path(self.path).resolve()),
            "file_name": Path(self.path).name,
            "source_hash": h.hexdigest()[:12],
            "line_count": len(self.lines),
            "files": files,
            "modules": self.modules,
            "packages": self.package_table(),
            "module_doc": (self.notebook and self.notebook.get("doc") or self.module_doc).strip(),
            "entry": self.entry,
            "imports": self.imports,
            "defs": defs_out,
            "sections": self.build_sections(),
            "lookups": self.lookups,
            "flows": flows,
            "nodes": self.nodes,
            "edges": list(self.edges.values()),
            "notebook": notebook,
            "tables": list(tables.values()),
        }

    def notebook_info(self):
        """Tag blocks with their cell, attach each cell's output to its last block, and find the two
        notebook traps: cells run out of document order, and names read before the cell that defines them."""
        nb = self.notebook
        last_in_cell = {}
        for i, nd in enumerate(self.nodes):
            c = cell_of(nb, nd["lines"][0])
            if not c:
                continue
            nd["cell"] = c["n"]
            if c["exec"] is not None:
                nd["exec"] = c["exec"]
            if c["n"] not in last_in_cell or nd["lines"][1] > self.nodes[last_in_cell[c["n"]]]["lines"][1]:
                last_in_cell[c["n"]] = i
        for c in nb["cells"]:
            if c["outputs"] and c["n"] in last_in_cell:
                self.nodes[last_in_cell[c["n"]]]["outputs"] = c["outputs"]
        written, forward, first_writer = set(), [], {}
        for nd in self.nodes:
            for w in nd.get("writes") or []:
                first_writer.setdefault(w, nd)
        for nd in self.nodes:
            for r in nd.get("reads") or []:
                w = first_writer.get(r)
                if r not in written and w is not None and w is not nd and w["lines"][0] > nd["lines"][0]:
                    forward.append({"var": r, "node": nd["id"], "cell": nd.get("cell"), "defined_in": w["id"], "defined_cell": w.get("cell")})
                    nd.setdefault("forward_reads", []).append(r)
                    self.add_edge(w["id"], nd["id"], r, "forward")
            written.update(nd.get("writes") or [])
        cells = [{k: v for k, v in c.items() if k != "outputs"} | {"has_output": bool(c["outputs"])} for c in nb["cells"]]
        return {"cells": cells, "code_cells": nb["code_cells"], "out_of_order": nb["out_of_order"], "forward_refs": forward}


def load_source(path: str) -> tuple[str, dict | None]:
    """The text the parser works on: the file itself, or a notebook flattened into a virtual script."""
    if Path(path).suffix.lower() == ".ipynb":
        return load_notebook(path)
    return Path(path).read_text(encoding="utf-8"), None


def parse_file(path: str) -> dict:
    if Path(path).suffix.lower() == ".sql":
        return sql_file_graph(path)
    source, nb = load_source(path)
    return GraphBuilder(source, path, nb).build()


def brief(graph: dict) -> str:
    """Compact text description of blocks and edges (no code), for an LLM."""
    out = [f"FILE: {graph['file_name']}  (source_hash {graph['source_hash']})"]
    if graph.get("entry"):
        e = graph["entry"]
        out.append(f"ENTRY: the pipeline is the body of {e['name']}() (lines {e['lines'][0]}-{e['lines'][1]}), called at line {e['call_line']}")
    nb = graph.get("notebook")
    if nb:
        out.append(f"NOTEBOOK: {nb['code_cells']} code cells read in document order; line numbers are of the flattened notebook, 'cell [k]' is the cell's execution count")
        if nb["out_of_order"]:
            out.append(f"  WARNING run order differs from document order at code cells {nb['out_of_order']}")
        for f in nb["forward_refs"]:
            out.append(f"  WARNING {f['var']} is read in {f['node']} (code cell {f['cell']}) but only defined later in {f['defined_in']} (code cell {f['defined_cell']}): works in a live kernel, breaks on Restart & Run All")
    if graph.get("modules"):
        out += ["", "LOCAL MODULES (the script's own code, read by the parser; their functions appear as blocks/flows under the names below):"]
        for m in graph["modules"]:
            out.append(f"  {m['module']} ({m['file']}): imported {', '.join(m['imported']) or '-'}" + (f"; also pulled in {', '.join(k for k in m['adopted'] if k not in m['imported'])}" if len(m['adopted']) > len(m['imported']) else "") + (f" — {m['doc']}" if m.get('doc') else ""))
    if graph.get("packages"):
        out += ["", "PACKAGES (third-party / stdlib, with the blocks that use them):"]
        for p in graph["packages"]:
            out.append(f"  {p['name']}" + (f" {p['version']}" if p.get("version") else "") + (f" — {p['summary']}" if p.get("summary") else "") + (f" | used by {','.join(p['used_by'])}" if p["used_by"] else " | imported, not used at top level"))
    out += ["", "BLOCKS:"]
    for n in graph["nodes"]:
        bits = [f"{n['id']}", n["kind"], repr(n["title"]), f"lines {n['lines'][0]}-{n['lines'][1]}"]
        if n.get("cell"):
            bits.append(f"cell [{n['exec']}]" if n.get("exec") is not None else f"code cell {n['cell']}")
        if n.get("def"):
            d = graph["defs"][n["def"]]
            bits.append(f"def at {d['lines'][0]}-{d['lines'][1]}")
            if d.get("doc"):
                bits.append(f"docstring: {d['doc']!r}")
        for key in ("reads", "global_reads", "writes"):
            if n.get(key):
                bits.append(f"{key}={','.join(n[key])}")
        if n.get("packages"):
            bits.append("uses=" + ",".join(n["packages"]))
        if n.get("file") and graph.get("modules"):
            bits.append(f"in {Path(n['file']).name}")
        if n.get("sql"):
            sq = n["sql"]
            bits.append("SQL" + (f" {sq['dialect']}" if sq["dialect"] else "") + f" {sq['kind']}" + (f" reads={','.join(sq['reads'])}" if sq["reads"] else "") + (f" writes={','.join(sq['writes'])}" if sq["writes"] else ""))
        bits.append(f"role?={n.get('role', 'support')}")
        out.append("  " + " | ".join(bits))
        for o in (n.get("outputs") or [])[:3]:
            if o.get("text"):
                shown = "\n".join(o["text"].splitlines()[:8])
                out.append(f"      output ({o['kind']}): " + shown.replace("\n", "\n      "))
    if graph.get("sections"):
        out += ["", "SECTIONS (from the script's banner comments):"]
        for sec in graph["sections"]:
            out.append(f"  {sec['id']} | {sec['title']!r} | lines {sec['lines'][0]}-{sec['lines'][1]} | blocks {','.join(sec['nodes'])}")
    if graph.get("lookups"):
        out += ["", "LOOKUPS (shown as tags, not arrows): " + ", ".join(l["name"] for l in graph["lookups"])]
    flows = graph.get("flows") or {}
    n_flow = sum(1 for f in flows.values() for n in f["nodes"] if n["kind"] != "input")
    out.append(f"\nNODE COUNT: {len(graph['nodes'])} blocks" + (f" + {n_flow} function-flow blocks = {len(graph['nodes']) + n_flow} ids to cover" if n_flow else ""))
    out += ["", "EDGES (source -> target: variables):"]
    for e in graph["edges"]:
        out.append(f"  {e['source']} -> {e['target']} [{e['kind']}]: {', '.join(e['vars'])}")
    flows = graph.get("flows") or {}
    sqlflows = {k: f for k, f in flows.items() if f.get("kind") == "sql"}
    if sqlflows:
        out += ["", "SQL QUERIES (the reader can open these in place; block ids are '<block>.sql/<id>': one block per CTE, 'out' is the final statement):"]
        for name, f in sqlflows.items():
            out.append(f"  {name}" + (f" [{f['dialect']}]" if f.get("dialect") else "") + (f" from file {f['file']}" if f.get("file") else ""))
            for n in f["nodes"]:
                if n["kind"] == "input":
                    out.append(f"    source tables: {', '.join(n['writes'])}")
                    continue
                out.append(f"    {name}/{n['id']} | {n['title']!r} | reads={','.join(n['reads'])} | role?={n.get('role', 'support')}")
    if graph.get("tables"):
        out += ["", "TABLES (SQL): " + "; ".join(f"{t['name']} read by {','.join(t['read_by']) or '-'} written by {','.join(t['written_by']) or '-'}" for t in graph["tables"])]
    if any(f.get("kind") != "sql" for f in flows.values()):
        out += ["", "FUNCTION FLOWS (the reader can open these in place; block ids are '<function>/<id>'):"]
        for name, f in flows.items():
            if f.get("kind") == "sql":
                continue
            out.append(f"  {name}({', '.join(f['params'])}) lines {f['lines'][0]}-{f['lines'][1]}")
            for n in f["nodes"]:
                if n["kind"] == "input":
                    continue
                bits = [f"{name}/{n['id']}", n["kind"], repr(n["title"]), f"lines {n['lines'][0]}-{n['lines'][1]}"]
                if n.get("arm"):
                    bits.append(f"arm[{n['arm']['label']}]")
                for key in ("reads", "writes"):
                    if n.get(key):
                        bits.append(f"{key}={','.join(n[key])}")
                if n.get("returns"):
                    bits.append("returns")
                bits.append(f"role?={n.get('role', 'support')}")
                out.append("    " + " | ".join(bits))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("script", help="Python file to analyse")
    ap.add_argument("-o", "--output", help="Write JSON here instead of stdout")
    ap.add_argument("--brief", action="store_true", help="Print a compact block/edge list (for Claude) instead of JSON")
    ap.add_argument("--source", action="store_true", help="Print the source the parser read (a notebook flattened to one script), with line numbers")
    args = ap.parse_args(argv)
    if args.source:
        src, _ = load_source(args.script)
        for i, l in enumerate(src.splitlines(), 1):
            print(f"{i:4d}  {l}")
        try:
            g = parse_file(args.script)
        except SyntaxError:
            return 0
        for m in g.get("modules") or []:
            print(f"\n==== {m['file']} (module {m['module']}; only the functions this script pulls in) ====")
            for key in m["adopted"]:
                d = g["defs"].get(key)
                if not d:
                    continue
                for i, l in enumerate(d["code"].splitlines(), d["lines"][0]):
                    print(f"{i:4d}  {l}")
                print()
        return 0
    try:
        graph = parse_file(args.script)
    except SyntaxError as e:
        err = {"error": f"SyntaxError: {e.msg}", "line": e.lineno}
        print(json.dumps(err), file=sys.stdout if not args.output else sys.stderr)
        return 1
    text = brief(graph) + "\n" if args.brief else json.dumps(graph, indent=1)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
