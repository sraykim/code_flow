#!/usr/bin/env python3
"""Code Flow renderer: build a standalone interactive HTML view of a script.

Usage:
    python codeflow_render.py script.py [-o out.html] [--enrich notes.json] [--open] [--vertical]
    python codeflow_render.py graph.json [-o out.html] [--enrich notes.json]

Without -o the page is written to .codeflow/<script>.flow.html next to the script.
The enrichment file (optional) holds Claude's summaries, stages and hidden
dependencies; see SKILL.md for its format. It is ignored if the script changed
since it was written.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from codeflow_parse import parse_file  # noqa: E402


def _js(obj) -> str:
    text = json.dumps(obj, ensure_ascii=False)
    return text.replace("</", "<\\/").replace("<!--", "<\\!--").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def render_html(graph: dict, enrichment: dict | None = None, template: Path | None = None,
                direction: str = "LR") -> str:
    html = (template or HERE / "viewer.html").read_text(encoding="utf-8")
    html = html.replace("/*__CODEFLOW_GRAPH__*/null", _js(graph), 1)
    html = html.replace("/*__CODEFLOW_ENRICH__*/null", _js(enrichment) if enrichment else "null", 1)
    html = html.replace("/*__CODEFLOW_OPTIONS__*/null", _js({"direction": direction}), 1)
    html = html.replace("<!--__CF_CSP__-->", "", 1)
    return html.replace("__CF_NONCE__", secrets.token_hex(12))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render a Code Flow HTML page.")
    ap.add_argument("input", help="A .py script, or a graph .json made by codeflow_parse.py")
    ap.add_argument("-o", "--output", help="Output .html path")
    ap.add_argument("--enrich", help="Claude enrichment JSON (summaries, stages, implicit edges)")
    ap.add_argument("--open", action="store_true", help="Open the page in the default browser")
    ap.add_argument("--vertical", action="store_true", help="Start in top-to-bottom layout (default is left-to-right; switchable in the page)")
    ap.add_argument("--horizontal", action="store_true", help=argparse.SUPPRESS)  # kept for old commands
    args = ap.parse_args(argv)

    src = Path(args.input)
    if src.suffix == ".json":
        graph = json.loads(src.read_text(encoding="utf-8"))
    else:
        try:
            graph = parse_file(str(src))
        except SyntaxError as e:
            print(f"SyntaxError in {src}: {e.msg} (line {e.lineno})", file=sys.stderr)
            return 1
    if graph.get("error"):
        print(graph["error"], file=sys.stderr)
        return 1

    enrichment = None
    if args.enrich and Path(args.enrich).exists():
        enrichment = json.loads(Path(args.enrich).read_text(encoding="utf-8"))
        if enrichment.get("source_hash") and enrichment["source_hash"] != graph["source_hash"]:
            print("note: enrichment was made for an older version of the script; ignoring it", file=sys.stderr)
            enrichment = None
        else:
            known = {n["id"] for n in graph["nodes"]}
            for fname, f in (graph.get("flows") or {}).items():
                known.update(f"{fname}/{n['id']}" for n in f["nodes"])
            bad = [k for k in (enrichment.get("nodes") or {}) if k not in known]
            if bad:
                print(f"note: enrichment mentions unknown block ids {bad}; they will be skipped", file=sys.stderr)

    script_path = Path(graph["file"])
    out = Path(args.output) if args.output else script_path.parent / ".codeflow" / f"{script_path.stem}.flow.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(graph, enrichment, direction="TB" if args.vertical else "LR"), encoding="utf-8")
    print(str(out))
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
