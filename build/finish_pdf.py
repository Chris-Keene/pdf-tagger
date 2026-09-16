#!/usr/bin/env python3
"""Close the last two PDF/UA gaps that deck-a11y's harden_pdf.py leaves on this page.

Two things survive Chrome's export plus hardening:

* **`LI` containing `H3`/`P` directly.** Chrome elides a plain wrapper `<div>`, so
  there is nothing for it to map to `LBody`, and PDF/UA 7.2 requires an `LI` to
  contain only `Lbl` and `LBody`. Fixed in the tree: insert an `LBody` and reparent.
* **Untagged decorative paint.** The step-number circles are CSS generated content
  (`::before` counters), which Chrome paints without tagging, so 7.1 requires them
  to be marked as artifacts. They are decoration, so `/Artifact` is the honest
  answer rather than inventing structure for them.

Run after harden_pdf.py.  Usage: finish_pdf.py in.pdf -o out.pdf
"""
import argparse
import sys

import pikepdf
from pikepdf import Array, Dictionary, Name, Operator

_OK_IN_LI = {"Lbl", "LBody"}
_PAINTS = {"f", "F", "f*", "B", "B*", "b", "b*", "S", "s", "sh", "Do",
           "Tj", "TJ", "'", '"', "EI"}
_MARK = {"BDC", "BMC"}


def fix_list_items(pdf):
    """Give every offending LI an LBody and move its content inside."""
    fixed = 0
    seen = set()

    def walk(node):
        nonlocal fixed
        try:
            oid = node.objgen
            if oid in seen:
                return
            seen.add(oid)
        except Exception:
            pass
        kids = node.get("/K")
        items = kids if isinstance(kids, Array) else ([kids] if kids is not None else [])
        if str(node.get("/S", "")).lstrip("/") == "LI":
            strays, keep = [], []
            for k in items:
                if isinstance(k, Dictionary) and str(k.get("/Type", "")) == "/StructElem":
                    if str(k.get("/S", "")).lstrip("/") in _OK_IN_LI:
                        keep.append(k)
                    else:
                        strays.append(k)
                else:
                    keep.append(k)
            if strays:
                body = pdf.make_indirect(Dictionary(
                    Type=Name.StructElem, S=Name.LBody, P=node, K=Array(strays)))
                for k in strays:
                    k["/P"] = body
                node["/K"] = Array(keep + [body])
                fixed += 1
                items = list(node["/K"])
        for k in items:
            if isinstance(k, Dictionary) and str(k.get("/Type", "")) == "/StructElem":
                walk(k)

    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return 0
    top = root.get("/K")
    for k in (top if isinstance(top, Array) else [top]):
        if isinstance(k, Dictionary):
            walk(k)
    return fixed


def _form_is_marked(xo, depth=0, seen=None):
    """Does this form XObject carry its own marked content?"""
    if depth > 4:
        return False
    seen = seen if seen is not None else set()
    try:
        if xo.objgen in seen:
            return False
        seen.add(xo.objgen)
    except Exception:
        pass
    try:
        ins = pikepdf.parse_content_stream(xo)
    except Exception:
        return False
    for item in ins:
        if str(item.operator) in _MARK:
            return True
    nested = (xo.get("/Resources") or {}).get("/XObject", {}) or {}
    for _n, v in nested.items():
        if str(v.get("/Subtype", "")) == "/Form" and _form_is_marked(v, depth + 1, seen):
            return True
    return False


def _wrap_unmarked(stream_owner):
    """Wrap top-level painting operators in /Artifact. Returns how many."""
    try:
        ins = pikepdf.parse_content_stream(stream_owner)
    except Exception:
        return 0, None
    xobjs = (stream_owner.get("/Resources") or {}).get("/XObject", {}) or {}
    depth = 0
    targets = []
    for i, item in enumerate(ins):
        op = str(item.operator)
        if op in _MARK:
            depth += 1
        elif op == "EMC":
            depth = max(0, depth - 1)
        elif depth == 0 and op in _PAINTS:
            # Never artifact a `Do` whose form holds its own marked content: that
            # puts tagged content inside an artifact (7.1 t2) and orphans it. The
            # form's own stream is handled separately instead.
            if op == "Do" and len(item.operands):
                nm = str(item.operands[0])
                xo = xobjs.get(nm)
                if xo is not None and str(xo.get("/Subtype", "")) == "/Form" \
                        and _form_is_marked(xo):
                    continue
            targets.append(i)
    if not targets:
        return 0, None
    # BDC/BMC are postfix operators, so build a new instruction list rather than
    # splicing bytes: getting that wrong corrupts the stream in a way that looks
    # like a semantic problem.
    out = []
    wrap = set(targets)
    for i, item in enumerate(ins):
        if i in wrap:
            out.append(pikepdf.ContentStreamInstruction([Name.Artifact], Operator("BMC")))
            out.append(item)
            out.append(pikepdf.ContentStreamInstruction([], Operator("EMC")))
        else:
            out.append(item)
    return len(targets), pikepdf.unparse_content_stream(out)


def artifact_stragglers(pdf):
    pages = forms = 0
    for page in pdf.pages:
        n, data = _wrap_unmarked(page)
        if data is not None:
            page.Contents = pdf.make_stream(data)
            pages += n
        seen = set()
        res = page.get("/Resources")
        for _name, xo in ((res or {}).get("/XObject", {}) or {}).items():
            if str(xo.get("/Subtype", "")) != "/Form":
                continue
            try:
                if xo.objgen in seen:
                    continue
                seen.add(xo.objgen)
            except Exception:
                pass
            n, data = _wrap_unmarked(xo)
            if data is not None:
                xo.write(data)
                forms += n
    return pages, forms


def unwrap_double_figures(pdf):
    """Demote a Figure whose only real content is another Figure.

    Chrome maps an HTML `<figure>` to a Figure element *and* the `<svg role="img">`
    inside it to a second one. The inner Figure carries the alt text; the outer is a
    container holding the figure and its caption, and having no alt of its own it
    fails PDF/UA 7.3. Fixing it in the tree keeps the HTML correct (figure and
    figcaption are the right markup for the page) instead of contorting the source
    to suit one exporter.
    """
    changed = 0
    seen = set()

    def walk(node):
        nonlocal changed
        try:
            oid = node.objgen
            if oid in seen:
                return
            seen.add(oid)
        except Exception:
            pass
        kids = node.get("/K")
        items = kids if isinstance(kids, Array) else ([kids] if kids is not None else [])
        if str(node.get("/S", "")).lstrip("/") == "Figure" and \
                not str(node.get("/Alt", "") or "").strip():
            child_types = [str(k.get("/S", "")).lstrip("/") for k in items
                           if isinstance(k, Dictionary)
                           and str(k.get("/Type", "")) == "/StructElem"]
            has_inner_figure = "Figure" in child_types
            only_containers = set(child_types) <= {"Figure", "Caption"}
            if has_inner_figure and only_containers and child_types:
                # A grouping element, not a figure. /Sect is the honest type.
                node["/S"] = Name.Sect
                changed += 1
        for k in items:
            if isinstance(k, Dictionary) and str(k.get("/Type", "")) == "/StructElem":
                walk(k)

    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return 0
    top = root.get("/K")
    for k in (top if isinstance(top, Array) else [top]):
        if isinstance(k, Dictionary):
            walk(k)
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args()
    pdf = pikepdf.open(a.pdf)
    lists = fix_list_items(pdf)
    figs = unwrap_double_figures(pdf)
    pages, forms = artifact_stragglers(pdf)
    pdf.save(a.out)
    print(f"  Lists:     {lists} LI given an LBody")
    print(f"  Figures:   {figs} container Figure(s) demoted to Sect")
    print(f"  Artifacts: {pages} in page streams, {forms} in Form XObjects")
    print(f"Wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
