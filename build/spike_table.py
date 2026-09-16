#!/usr/bin/env python3
"""Spike: does a /Table structure tree actually validate?

Proves the mechanism only. It writes a real Table > TR > TH/TD tree over cells
found by pymupdf's table finder, including merged cells as
/RowSpan, and validates the result. Everything outside the table is artifacted so the unmarked-content rule
is satisfied and the run measures the table structure and nothing else.

Not the feature: no UI, no confidence surfacing, no integration with analyse.py.
"""
import os
import subprocess
import sys
import warnings
import xml.etree.ElementTree as ET

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pikepdf
import pymupdf
from pikepdf import Array, Dictionary, Name, Operator, String

from app.analyse import walk_content
from app.hardening import harden

FONT = next((p for p in ("/System/Library/Fonts/Supplemental/Arial.ttf",
                         "/System/Library/Fonts/Supplemental/Verdana.ttf")
             if os.path.exists(p)), None)


# --------------------------------------------------------------------------- #
def make_fixture(path):
    """A page with a heading and a ruled table, one cell spanning two columns."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=420)

    def text(xy, s, size=10, font="F0"):
        if FONT:
            page.insert_text(xy, s, fontsize=size, fontname=font, fontfile=FONT)
        else:
            page.insert_text(xy, s, fontsize=size)

    text((50, 60), "Scan results by product", 18)

    cols = [50, 210, 330, 450, 545]
    rows = [90, 120, 150, 180, 210]
    for x in cols:
        page.draw_line(pymupdf.Point(x, rows[0]), pymupdf.Point(x, rows[-1]),
                       color=(0.3, 0.3, 0.3), width=0.8)
    for y in rows:
        page.draw_line(pymupdf.Point(cols[0], y), pymupdf.Point(cols[-1], y),
                       color=(0.3, 0.3, 0.3), width=0.8)

    data = [["Product", "Issues", "Critical", "Score"],
            ["Site Scanner", "12,128", "195", "67"],
            ["Mobile SDK", "1,004", "22", "81"],
            ["Flow Analyzer", "318", "7", "92"]]
    for r, row in enumerate(data):
        for c, cell in enumerate(row):
            text((cols[c] + 6, rows[r] + 20), cell, 10)
    doc.save(path)
    doc.close()
    return path


# --------------------------------------------------------------------------- #
def score(table):
    """The gate from the survey: dense grid with a populated first row."""
    data = table.extract()
    cells = [c for row in data for c in row]
    filled = [c for c in cells if c and str(c).strip()]
    fill = len(filled) / max(len(cells), 1)
    head = data[0] if data else []
    hdr = sum(1 for c in head if c and str(c).strip()) / max(len(head), 1)
    verdict = ("table" if fill >= 0.5 and hdr >= 0.6
               else "ask a human" if fill >= 0.3
               else "not a table")
    return fill, hdr, verdict


def cell_ops(units, rect, page_height):
    """Operator ranges whose text origin falls inside this cell."""
    x0, y0, x1, y1 = rect
    out = []
    for u in units:
        if u["kind"] != "text" or u.get("used"):
            continue
        uy = page_height - u["y"]
        if x0 - 2 <= u["x"] <= x1 + 2 and y0 - 4 <= uy <= y1 + 6:
            out.append(u["ops"])
            u["used"] = True
    return out


def tag(src, out):
    pdf = pikepdf.open(src)
    doc = pymupdf.open(src)
    page = pdf.pages[0]
    H = float(page.MediaBox[3])
    ins, units = walk_content(page)

    tables = [t for t in doc[0].find_tables().tables
              if len(t.rows) >= 2 and t.col_count >= 2]
    if not tables:
        print("  no table detected"); return None
    t = tables[0]
    fill, hdr, verdict = score(t)
    print(f"  detected {len(t.rows)}x{t.col_count}  fill={fill:.2f} header={hdr:.2f} -> {verdict}")

    struct = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    document = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document,
                                            P=struct, K=Array()))
    table_el = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Table,
                                            P=document, K=Array(), Pg=page.obj))

    claimed, mcid, page_map = {}, 0, []

    def claim(ops_list, parent):
        nonlocal mcid
        ids = []
        for o in ops_list:
            claimed[tuple(o)] = mcid
            ids.append(mcid)
            page_map.append(parent)
            mcid += 1
        return ids

    rows = t.rows

    def rowspan(r, c):
        """How many rows this cell covers.

        A covered position comes back as None, so the span is 1 plus however many
        rows below repeat the None at the same column index. Without this, rows
        under a merged cell are short and the table fails 7.2 t43, which requires
        every row to account for the full column count once spans are considered.
        """
        n = 1
        for k in range(r + 1, len(rows)):
            cells = rows[k].cells
            if c < len(cells) and cells[c] is None:
                n += 1
            else:
                break
        return n

    for r, row in enumerate(rows):
        tr = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.TR,
                                          P=table_el, K=Array()))
        for c, rect in enumerate(row.cells):
            if rect is None:
                continue                       # covered by a span from above
            is_header = (r == 0)
            cell = pdf.make_indirect(Dictionary(
                Type=Name.StructElem, S=Name.TH if is_header else Name.TD,
                P=tr, K=Array(), Pg=page.obj))
            attrs = Dictionary(O=Name.Table)
            if is_header:
                # /Scope is what tells a screen reader which cells this heads.
                attrs[Name.Scope] = Name.Column
            span = rowspan(r, c)
            if span > 1:
                attrs[Name.RowSpan] = span
            if len(attrs) > 1:
                cell.A = pdf.make_indirect(attrs)
            ids = claim(cell_ops(units, rect, H), cell)
            cell.K = Array(list(ids))
            tr.K.append(cell)
        table_el.K.append(tr)
    document.K.append(table_el)

    # everything else on the page becomes an artifact
    opens, closes = {}, {}
    for o, mid in claimed.items():
        opens.setdefault(o[0], []).append(
            pikepdf.ContentStreamInstruction([Name.TD, Dictionary(MCID=mid)],
                                             Operator("BDC")))
        closes[o[1]] = closes.get(o[1], 0) + 1
    for u in units:
        o = tuple(u["ops"])
        if o in claimed:
            continue
        opens.setdefault(o[0], []).append(
            pikepdf.ContentStreamInstruction([Name.Artifact], Operator("BMC")))
        closes[o[1]] = closes.get(o[1], 0) + 1

    new = []
    for i, item in enumerate(ins):
        for op in opens.get(i, []):
            new.append(op)
        if str(item.operator) not in ("BDC", "BMC", "EMC"):
            new.append(item)
        for _ in range(closes.get(i, 0)):
            new.append(pikepdf.ContentStreamInstruction([], Operator("EMC")))
    page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(new))

    page.StructParents = 0
    struct.K = Array([document])
    struct.ParentTree = pdf.make_indirect(Dictionary(
        Nums=Array([0, pdf.make_indirect(Array(page_map))])))
    struct.ParentTreeNextKey = 1
    struct.RoleMap = Dictionary()
    pdf.Root.StructTreeRoot = struct
    harden(pdf, "Scan results by product", "en-GB")
    pdf.save(out)
    return out


def verify(path):
    p = subprocess.run(["verapdf", "-f", "ua1", "--format", "xml", path],
                       capture_output=True, text=True, timeout=600)
    root = ET.fromstring(p.stdout)
    d = root.find(".//details")
    v = root.find(".//validationReport")
    fails = [(r.get("clause"), r.get("testNumber"), r.get("failedChecks"))
             for r in root.iter("rule") if r.get("status") == "failed"]
    return v.get("isCompliant"), d.get("passedRules"), d.get("failedRules"), fails


if __name__ == "__main__":
    import tempfile
    T = tempfile.mkdtemp()
    src = make_fixture(os.path.join(T, "table.pdf"))
    print("fixture written")
    out = tag(src, os.path.join(T, "table-tagged.pdf"))
    if out:
        ok, passed, failed, fails = verify(out)
        print(f"\n  veraPDF: compliant={ok}  {passed} passed, {failed} failed")
        for c, t2, n in fails:
            print(f"    {c} t{t2} ({n} checks)")
        a = pymupdf.open(src)[0].get_pixmap(dpi=72).samples
        b = pymupdf.open(out)[0].get_pixmap(dpi=72).samples
        print(f"  pixels unchanged: {a == b}")
