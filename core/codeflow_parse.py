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

SCHEMA_VERSION = 3
BUILTIN_NAMES = set(dir(builtins))


# --------------------------------------------------------------------------
# Name collection
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# Graph builder
# --------------------------------------------------------------------------
class GraphBuilder:
    def __init__(self, source: str, path: str):
        self.source = source
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

    # ---------- helpers ----------
    def seg(self, start, end):
        return "\n".join(self.lines[start - 1:end])

    def new_id(self, prefix):
        self._counter += 1
        return f"{prefix}{self._counter}"

    def leading_comment(self, lineno):
        """Comment lines directly above `lineno` (no blank line between)."""
        out, i = [], lineno - 2
        while i >= 0 and self.lines[i].strip().startswith("#"):
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

    def is_local_call(self, node):
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in self.defs
        )

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
                for a in st.names:
                    self.imported.add((a.asname or a.name).split(".")[0])
                self.imports.append({"line": st.lineno, "code": self.seg(st.lineno, st.end_lineno)})
            elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.defs[st.name] = {"node": st}
            else:
                module_names.update(collect(st).writes)
        self.module_names = module_names - set(self.defs) - self.imported

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
        for nd in self.nodes:
            ln = nd["lines"][0]
            target = secs[0]
            for sec in secs:
                if ln >= sec["lines"][0]:
                    target = sec
            target["nodes"].append(nd["id"])
        secs = [sec for sec in secs if sec["nodes"]]
        if len(secs) < 2:
            return []
        for i, sec in enumerate(secs, 1):
            sec["id"] = f"s{i}"
        return secs

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
        d = self.defs[call.func.id]
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
        }
        if isinstance(st, ast.Return) or any(isinstance(x, ast.Return) for x in ast.walk(st)):
            node["returns"] = True
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
            self.mark_lookups(config_params=config)
            nodes, edges, lookups = self.nodes, list(self.edges.values()), self.lookups
        finally:
            (self.nodes, self.edges, self.writers, self.pending, self._counter, self.arms, self.in_flow) = saved
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
            if d["kind"] in ("function", "async"):
                f = self.build_flow(d)
                if f:
                    flows[name] = f
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
        self.section_heads = self.find_section_heads()
        self.walk(self.tree.body, top_level=True)
        self.add_helpers()
        self.mark_lookups()
        self.retitle()
        flows = self.build_flows()
        defs_out = {
            name: {k: v for k, v in d.items() if k not in ("node", "used_at_top", "used_inside_code")}
            for name, d in self.defs.items()
        }
        return {
            "schema": SCHEMA_VERSION,
            "file": str(Path(self.path).resolve()),
            "file_name": Path(self.path).name,
            "source_hash": hashlib.sha1(self.source.encode()).hexdigest()[:12],
            "line_count": len(self.lines),
            "module_doc": self.module_doc.strip(),
            "entry": self.entry,
            "imports": self.imports,
            "defs": defs_out,
            "sections": self.build_sections(),
            "lookups": self.lookups,
            "flows": flows,
            "nodes": self.nodes,
            "edges": list(self.edges.values()),
        }


def parse_file(path: str) -> dict:
    source = Path(path).read_text(encoding="utf-8")
    return GraphBuilder(source, path).build()


def brief(graph: dict) -> str:
    """Compact text description of blocks and edges (no code), for an LLM."""
    out = [f"FILE: {graph['file_name']}  (source_hash {graph['source_hash']})"]
    if graph.get("entry"):
        e = graph["entry"]
        out.append(f"ENTRY: the pipeline is the body of {e['name']}() (lines {e['lines'][0]}-{e['lines'][1]}), called at line {e['call_line']}")
    out += ["", "BLOCKS:"]
    for n in graph["nodes"]:
        bits = [f"{n['id']}", n["kind"], repr(n["title"]), f"lines {n['lines'][0]}-{n['lines'][1]}"]
        if n.get("def"):
            d = graph["defs"][n["def"]]
            bits.append(f"def at {d['lines'][0]}-{d['lines'][1]}")
            if d.get("doc"):
                bits.append(f"docstring: {d['doc']!r}")
        for key in ("reads", "global_reads", "writes"):
            if n.get(key):
                bits.append(f"{key}={','.join(n[key])}")
        out.append("  " + " | ".join(bits))
    if graph.get("sections"):
        out += ["", "SECTIONS (from the script's banner comments):"]
        for sec in graph["sections"]:
            out.append(f"  {sec['id']} | {sec['title']!r} | lines {sec['lines'][0]}-{sec['lines'][1]} | blocks {','.join(sec['nodes'])}")
    if graph.get("lookups"):
        out += ["", "LOOKUPS (shown as tags, not arrows): " + ", ".join(l["name"] for l in graph["lookups"])]
    out += ["", "EDGES (source -> target: variables):"]
    for e in graph["edges"]:
        out.append(f"  {e['source']} -> {e['target']} [{e['kind']}]: {', '.join(e['vars'])}")
    flows = graph.get("flows") or {}
    if flows:
        out += ["", "FUNCTION FLOWS (the reader can open these in place; block ids are '<function>/<id>'):"]
        for name, f in flows.items():
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
                out.append("    " + " | ".join(bits))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("script", help="Python file to analyse")
    ap.add_argument("-o", "--output", help="Write JSON here instead of stdout")
    ap.add_argument("--brief", action="store_true", help="Print a compact block/edge list (for Claude) instead of JSON")
    args = ap.parse_args(argv)
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
