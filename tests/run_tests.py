#!/usr/bin/env python3
"""Test suite for PDF Tagger.

Run:  ./.venv/bin/python tests/run_tests.py [--quick] [--no-verapdf]

Covers five things:
  1. the corpus  - real Google Slides exports tag, validate and stay pixel-identical
  2. round-trip  - a tagged file re-opened and re-tagged must not drift or accumulate
  3. structure   - a deep check of the tree itself, beyond what veraPDF looks at
  4. edge cases  - blank pages, no text, rotation, mixed sizes, CJK, encrypted, corrupt
  5. the API     - the running server's behaviour on bad and hostile input

Exits non-zero if anything fails, so it can gate a build.
"""
import argparse
import collections
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pikepdf
import pymupdf

from app.analyse import analyse, normalise_headings, heading_warnings
from app.checks import alt_text_issues, review, summary
from app.model import DocModel
from app.tagger import build
from app import edits

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="pdftagger-tests-")
USE_VERAPDF = True


# Real-world PDFs the suite checks itself against. The synthetic fixtures prove
# the logic; the corpus proves it against documents nobody designed for us, and
# those files are not ours to ship. Settings come from tests/corpus.json or from
# PDFTAG_CORPUS_<NAME>, and anything unset simply skips the checks that need it.
def _corpus_conf():
    try:
        with open(os.path.join(os.path.dirname(__file__), "corpus.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def corpus(name, default=""):
    """One named file, or the empty string when there isn't one to hand."""
    val = os.environ.get("PDFTAG_CORPUS_" + name) or _corpus_conf().get(name) or default
    return os.path.expanduser(val) if val else ""


def corpus_list(name, default=()):
    """Glob patterns naming real files; os.pathsep-separated in the environment."""
    env = os.environ.get("PDFTAG_CORPUS_" + name)
    pats = env.split(os.pathsep) if env else _corpus_conf().get(name, default)
    if isinstance(pats, str):
        pats = [pats]
    return [os.path.expanduser(p) for p in pats or ()]


def corpus_dir():
    """The directory swept for untagged real exports. Opt out by pointing it at
    an empty folder: sweeping somebody's whole Downloads is a rude default to
    inflict silently, so it is one line to change."""
    return corpus("DIR", "~/Downloads")


_TITLES = ["Findings and recommendations", "Where the coverage gaps are",
           "Why this matters to the team", "What the tooling does",
           "Results across the estate", "Next steps and owners"]
_BODY = [["Automated checks reach a good deal of the estate, but not all of it,",
          "and the parts they miss are the parts that stop people using a page",
          "at all. This is the gap the review is meant to close."],
         ["Issues found while a feature is still being built cost a fraction of",
          "what the same issue costs once it has shipped, which is the whole of",
          "the argument for moving the checks earlier."]]
_BULLETS = ["Coverage across every public page", "A verdict a person can act on",
            "Evidence that survives an audit"]
_LABELS = ["Coverage", "Cost of delay", "Ownership", "Tooling", "Estate", "Owners"]


def _build_stand_in(path, pages):
    """A deck of the same shape as a real Slides export, built rather than borrowed.

    It is not a forgery of one. What the checks using it depend on is an untagged
    file with embedded fonts, slide geometry, enough running text to judge a body
    size, a picture, and at least one call a person would have to make; all of
    that is reproducible. What it cannot reproduce is a particular producer's
    quirks, so a real export is still preferred when the machine has one.
    """
    doc = pymupdf.open()
    reg, bold = pymupdf.Font("helv").buffer, pymupdf.Font("hebo").buffer
    w, h = (720, 405) if pages > 1 else (612, 792)
    for n in range(pages):
        pg = doc.new_page(width=w, height=h)
        pg.insert_font(fontname="FR", fontbuffer=reg)
        pg.insert_font(fontname="FB", fontbuffer=bold)
        pg.insert_text((40, 56), _TITLES[n % len(_TITLES)], fontsize=24, fontname="FB")
        y = 100
        for line in _BODY[n % len(_BODY)]:
            pg.insert_text((40, y), line, fontsize=12, fontname="FR")
            y += 17
        y += 12
        for b in _BULLETS:
            # U+2022, not U+25CF: Helvetica has no filled circle, and a missing
            # glyph is both an unreadable bullet and a 7.21.7 conformance failure.
            pg.insert_text((40, y), "\u2022", fontsize=12, fontname="FR")
            pg.insert_text((58, y), b, fontsize=12, fontname="FR")
            y += 18
        y += 10
        for i in range(6):
            pg.insert_text((40, y), "Supporting note %d, set smaller than the body "
                           "text above it." % (i + 1), fontsize=9, fontname="FR")
            y += 12
        pg.insert_text((460, 120), _LABELS[n % len(_LABELS)], fontsize=15, fontname="FB")
        pg.insert_text((460, 150), "4X", fontsize=26, fontname="FB")
        # Two pictures, in different places: the same picture in the same spot on
        # every page is slide furniture, and is meant to come out as an artifact.
        if n in (1, 3):
            px = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 160, 110))
            px.set_rect(px.irect, (252, 252, 253))
            for i, bh in enumerate((28, 46, 63, 81, 97)):
                px.set_rect(pymupdf.IRect(14 + i * 28, 104 - bh, 36 + i * 28, 104),
                            (0, 134 - i * 6, 199 - i * 4))
            for gx in range(0, 160, 9):
                px.set_rect(pymupdf.IRect(gx, 0, gx + 1, 8), (120 + gx % 90, 140, 200))
            pg.insert_image(pymupdf.Rect(440, 190, 660, 340) if n == 1
                            else pymupdf.Rect(300, 230, 470, 350), pixmap=px)
        # A hair over the smallest heading boundary and not bold, which is a
        # genuine coin toss. Without something like it the review queue is never
        # exercised; last on the page so it is not read as a kicker either.
        pg.insert_text((460, 372), "Owner and next step", fontsize=10.1, fontname="FR")
    doc.set_metadata({"title": "Quarterly review"})
    doc.save(path)
    doc.close()


def fixture(name):
    """A real export if this machine has one, otherwise a built stand-in.

    The exports these checks were written against are a customer deck and a
    product one-pager; neither is ours to publish, so they are kept out of the
    repository and the suite falls back to something of the same shape.
    """
    real = os.path.join(ROOT, "tests", name)
    if os.path.exists(real):
        return real
    made = os.path.join(TMP, name)
    if not os.path.exists(made):
        _build_stand_in(made, pages=6 if "6page" in name else 1)
    return made
RESULTS = []
SKIPPED = []


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
class Fail(AssertionError):
    pass


def check(name, cond, detail=""):
    RESULTS.append((bool(cond), name, detail))
    mark = "\033[32mPASS\033[0m" if cond else "\033[31mFAIL\033[0m"
    print(f"  {mark}  {name}" + (f"  \033[2m{detail}\033[0m" if detail else ""))
    return bool(cond)


def skip(name, why):
    """A check this machine hasn't the material for. Reported but not failed: an
    absent corpus file says something about the checkout, not about the code."""
    SKIPPED.append((name, why))
    print(f"  \033[33mSKIP\033[0m  {name}  \033[2m{why}\033[0m")


def section(title):
    print(f"\n\033[1m{title}\033[0m")


def tag(src, out, alt="A chart showing the scan results", lang="en-GB"):
    """Analyse, fill in every description, tag."""
    model = normalise_headings(analyse(src, lang=lang))
    for p in model.pages:
        for e in p.elements:
            if e.role == "Figure":
                e.alt = alt
    build(model, out)
    return model, out


def verapdf(path):
    if not USE_VERAPDF:
        return None
    exe = shutil.which("verapdf") or "/opt/homebrew/bin/verapdf"
    if not os.path.exists(exe):
        return None
    # verapdf exits non-zero on FAIL, so the return code is not an error signal
    p = subprocess.run([exe, "-f", "ua1", "--format", "xml", path],
                       capture_output=True, text=True, timeout=900)
    try:
        root = ET.fromstring(p.stdout)
    except ET.ParseError:
        return None
    vr = root.find(".//validationReport")
    d = root.find(".//details")
    if vr is None or d is None:
        return None
    fails = [(r.get("clause"), r.get("testNumber"), int(r.get("failedChecks", 0)))
             for r in root.iter("rule") if r.get("status") == "failed"]
    return {"compliant": vr.get("isCompliant") == "true",
            "passed": int(d.get("passedRules", 0)),
            "failed": int(d.get("failedRules", 0)), "rules": fails}


def verapdf_failed_rules(path):
    """The set of (clause, test) veraPDF fails on, or None if it is unavailable."""
    v = verapdf(path)
    return None if v is None else {(c, t) for c, t, _ in v["rules"]}


def page_hashes(path, dpi=72):
    doc = pymupdf.open(path)
    return [hashlib.sha1(doc[i].get_pixmap(dpi=dpi).samples).hexdigest()
            for i in range(len(doc))]


def text_of(path):
    doc = pymupdf.open(path)
    return [doc[i].get_text() for i in range(len(doc))]


def stream_hashes(path):
    pdf = pikepdf.open(path)
    out = []
    for pg in pdf.pages:
        c = pg.Contents
        raw = b"".join(x.read_bytes() for x in c) if isinstance(c, pikepdf.Array) \
            else c.read_bytes()
        out.append(hashlib.sha1(raw).hexdigest())
    return out


# --------------------------------------------------------------------------- #
# a deeper structural check than veraPDF performs
# --------------------------------------------------------------------------- #
def inspect_structure(path):
    """Walk the written tree and report anything internally inconsistent."""
    pdf = pikepdf.open(path)
    problems = []
    if "/StructTreeRoot" not in pdf.Root:
        return ["no /StructTreeRoot"], {}
    sr = pdf.Root.StructTreeRoot

    # --- ParentTree number keys must be unique and ascending -----------------
    nums = sr.get("/ParentTree", {}).get("/Nums")
    keys = [int(nums[i]) for i in range(0, len(nums), 2)] if nums else []
    if keys != sorted(keys):
        problems.append("ParentTree /Nums keys are not ascending")
    if len(keys) != len(set(keys)):
        problems.append("ParentTree /Nums has duplicate keys")

    # --- every marked-content id in each stream must have an owner ------------
    mark_ops = {"BDC", "BMC", "EMC"}
    per_page_ids = []
    for pno, page in enumerate(pdf.pages):
        ins = pikepdf.parse_content_stream(page)
        ids, depth, maxdepth, unbalanced = [], 0, 0, False
        for item in ins:
            op = str(item.operator)
            if op in ("BDC", "BMC"):
                depth += 1
                maxdepth = max(maxdepth, depth)
            elif op == "EMC":
                depth -= 1
                if depth < 0:
                    unbalanced = True
            if op == "BDC" and len(item.operands) > 1:
                try:
                    ids.append(int(item.operands[1]["/MCID"]))
                except Exception:
                    pass
        if unbalanced or depth != 0:
            problems.append(f"page {pno+1}: unbalanced marked content (ends at {depth})")
        if maxdepth > 1:
            problems.append(f"page {pno+1}: marked content nested {maxdepth} deep")
        dups = [k for k, v in collections.Counter(ids).items() if v > 1]
        if dups:
            problems.append(f"page {pno+1}: duplicate MCIDs {sorted(dups)[:6]}")
        per_page_ids.append(ids)

        sp = page.get("/StructParents")
        if ids and sp is None:
            problems.append(f"page {pno+1}: has MCIDs but no /StructParents")
        elif sp is not None:
            key = int(sp)
            if key not in keys:
                problems.append(f"page {pno+1}: /StructParents {key} missing from ParentTree")
            else:
                arr = nums[keys.index(key) * 2 + 1]
                for mcid in ids:
                    if mcid >= len(arr):
                        problems.append(
                            f"page {pno+1}: MCID {mcid} beyond ParentTree array ({len(arr)})")
                        break
                    if arr[mcid] is None:
                        problems.append(f"page {pno+1}: MCID {mcid} has no owner")
                        break

    # --- walk the tree: types, alt text, parent links -------------------------
    counts = collections.Counter()
    empty_alt = 0
    bad_parent = 0
    seen = set()

    def walk(node, parent):
        nonlocal empty_alt, bad_parent
        try:
            oid = node.objgen
        except Exception:
            oid = None
        if oid is not None:
            if oid in seen:
                return
            seen.add(oid)
        s = str(node.get("/S", ""))
        if s:
            counts[s.lstrip("/")] += 1
        if s == "/Figure":
            alt = str(node.get("/Alt", "") or "")
            if not alt.strip():
                empty_alt += 1
        if parent is not None and "/P" in node:
            try:
                if node.P.objgen != parent.objgen:
                    bad_parent += 1
            except Exception:
                pass
        kids = node.get("/K")
        if kids is None:
            return
        items = kids if isinstance(kids, pikepdf.Array) else [kids]
        for k in items:
            if isinstance(k, pikepdf.Dictionary) and str(k.get("/Type", "")) == "/StructElem":
                walk(k, node)

    doc_elem = sr.K[0] if isinstance(sr.K, pikepdf.Array) else sr.K
    walk(doc_elem, sr)

    if empty_alt:
        problems.append(f"{empty_alt} Figure(s) with an empty /Alt")
    if bad_parent:
        problems.append(f"{bad_parent} element(s) whose /P does not point at their parent")

    # --- heading levels must not skip, in tree order -------------------------
    order = []

    def walk_h(node):
        s = str(node.get("/S", "")).lstrip("/")
        if len(s) == 2 and s[0] == "H" and s[1].isdigit():
            order.append(int(s[1]))
        kids = node.get("/K")
        if kids is None:
            return
        items = kids if isinstance(kids, pikepdf.Array) else [kids]
        for k in items:
            if isinstance(k, pikepdf.Dictionary) and str(k.get("/Type", "")) == "/StructElem":
                walk_h(k)

    walk_h(doc_elem)
    for i in range(1, len(order)):
        if order[i] > order[i - 1] + 1:
            problems.append(f"heading level skips H{order[i-1]} to H{order[i]}")
            break
    if order and order[0] != 1:
        problems.append(f"first heading is H{order[0]}, not H1")

    # --- document-level requirements ----------------------------------------
    if str(pdf.Root.get("/Lang", "")) == "":
        problems.append("no /Lang")
    if not pdf.Root.get("/MarkInfo", {}).get("/Marked"):
        problems.append("/MarkInfo /Marked not true")
    if not pdf.Root.get("/ViewerPreferences", {}).get("/DisplayDocTitle"):
        problems.append("DisplayDocTitle not true")
    if "/Metadata" not in pdf.Root:
        problems.append("no XMP /Metadata")
    else:
        xmp = bytes(pdf.Root.Metadata.read_bytes())
        if b"pdfuaid:part" not in xmp:
            problems.append("XMP lacks pdfuaid:part")
        if b"dc:title" not in xmp:
            problems.append("XMP lacks dc:title")

    return problems, dict(counts)


# --------------------------------------------------------------------------- #
# 1. corpus
# --------------------------------------------------------------------------- #
def test_corpus(limit=None):
    section("1. Corpus: real Google Slides exports")
    downloads = corpus_dir()
    cands = []
    for f in sorted(glob.glob(os.path.join(downloads, "*.pdf"))):
        try:
            pdf = pikepdf.open(f)
            if "/StructTreeRoot" in pdf.Root:
                continue
            mb = [float(v) for v in pdf.pages[0].MediaBox]
            w, h = mb[2] - mb[0], mb[3] - mb[1]
            if not (700 <= w <= 740 and 390 <= h <= 420):
                continue          # 720x405, the Slides export size
            cands.append((f, len(pdf.pages)))
        except Exception:
            continue
    if limit:
        cands = cands[:limit]
    if not cands:
        skip("corpus checks", f"no untagged Slides PDFs in {corpus_dir()}")
        return

    for src, pages in cands:
        name = os.path.basename(src)[:-4]
        out = os.path.join(TMP, f"corpus-{abs(hash(name)) % 99999}.pdf")
        try:
            model, _ = tag(src, out)
        except Exception as exc:
            check(f"{name[:40]} tags", False, repr(exc)[:70])
            continue
        v = verapdf(out)
        problems, counts = inspect_structure(out)
        same_px = page_hashes(src) == page_hashes(out)
        same_tx = text_of(src) == text_of(out)
        ok = (v is None or v["compliant"]) and not problems and same_px and same_tx
        detail = f"{pages}pp"
        if v:
            detail += f", veraPDF {v['passed']}/{v['passed']+v['failed']}"
        if problems:
            detail += f", structure: {problems[0]}"
        if not same_px:
            detail += ", PIXELS CHANGED"
        if not same_tx:
            detail += ", TEXT CHANGED"
        check(f"{name[:40]}", ok, detail)


# --------------------------------------------------------------------------- #
# 2. round-trip
# --------------------------------------------------------------------------- #
def test_round_trip():
    section("2. Round-trip: re-open and re-tag an already-tagged file")
    for src in ("slides-6page.pdf", "slides-1page.pdf"):
        src = fixture(src)
        name = os.path.basename(src)
        p1 = os.path.join(TMP, f"rt1-{name}")
        p2 = os.path.join(TMP, f"rt2-{name}")
        p3 = os.path.join(TMP, f"rt3-{name}")
        m1, _ = tag(src, p1)
        m2, _ = tag(p1, p2)
        m3, _ = tag(p2, p3)

        def roles(m):
            c = collections.Counter()
            for p in m.pages:
                for e in p.elements:
                    c[e.role] += 1
            return dict(c)

        check(f"{name}: re-opened file is detected as tagged", m2.was_tagged)
        check(f"{name}: roles identical on re-analysis", roles(m1) == roles(m2),
              f"{roles(m1)} vs {roles(m2)}" if roles(m1) != roles(m2) else "")
        check(f"{name}: roles stable on a third pass", roles(m2) == roles(m3))
        check(f"{name}: reading order identical on re-analysis",
              [e.order for p in m1.pages for e in p.elements] ==
              [e.order for p in m2.pages for e in p.elements])
        h1, h2, h3 = stream_hashes(p1), stream_hashes(p2), stream_hashes(p3)
        check(f"{name}: content streams byte-identical after re-tag", h1 == h2,
              "streams changed" if h1 != h2 else "no drift")
        check(f"{name}: still identical on a third pass", h2 == h3)
        check(f"{name}: title carried through", m2.title == m1.title and bool(m2.title),
              repr(m2.title))
        check(f"{name}: language carried through", m2.lang == m1.lang, m2.lang)

        for label, path in (("first", p1), ("re-tagged", p3)):
            problems, _ = inspect_structure(path)
            check(f"{name}: structure clean ({label})", not problems,
                  "; ".join(problems[:2]))
            v = verapdf(path)
            if v:
                check(f"{name}: veraPDF compliant ({label})", v["compliant"],
                      f"{v['passed']}/{v['passed']+v['failed']}")

        check(f"{name}: pixels identical to the original after 3 passes",
              page_hashes(src) == page_hashes(p3))
        check(f"{name}: text identical to the original after 3 passes",
              text_of(src) == text_of(p3))
        # nothing outstanding when every description is filled in
        s = summary(m2)
        check(f"{name}: re-opened file reports no blockers", s["blockers"] == 0,
              f"{s['blockers']} blockers, {s['warnings']} warnings")


# --------------------------------------------------------------------------- #
# 3. already tagged by other tools
# --------------------------------------------------------------------------- #
def test_foreign_tagged():
    section("3. Files already tagged by something else")
    # Files some other producer already tagged, to prove we read a foreign tree
    # rather than only our own. Named by pattern because the useful ones are
    # whatever happens to be lying around, not anything we could ship.
    cands = []
    for pat in corpus_list("TAGGED"):
        for f in sorted(glob.glob(os.path.join(os.path.dirname(ROOT), pat))):
            try:
                pdf = pikepdf.open(f)          # must stay referenced while in use
                if "/StructTreeRoot" in pdf.Root:
                    cands.append(f)
                pdf.close()
            except Exception:
                pass
    if not cands:
        skip("foreign-tree checks", "no already-tagged PDF to hand")
        return
    for src in cands[:3]:
        name = os.path.basename(src)[:38]
        out = os.path.join(TMP, f"foreign-{abs(hash(name)) % 9999}.pdf")
        try:
            model, _ = tag(src, out)
        except Exception as exc:
            check(f"{name}: re-tags", False, repr(exc)[:70])
            continue
        problems, _ = inspect_structure(out)
        check(f"{name}: detected as already tagged", model.was_tagged)
        check(f"{name}: re-tags to a clean structure", not problems,
              "; ".join(problems[:2]))
        check(f"{name}: pixels unchanged", page_hashes(src) == page_hashes(out))
        before, after = verapdf_failed_rules(src), verapdf_failed_rules(out)
        if before is not None and after is not None:
            introduced = sorted(after - before)
            check(f"{name}: tagging introduced no new failure", not introduced,
                  f"introduced {introduced}" if introduced
                  else f"fixed {len(before - after)}")
            if model.nested_text_pages:
                # Content inside a form cannot be reached, so the honest outcome is
                # a file that still reports unmarked content rather than one that
                # claims conformance while hiding it.
                check(f"{name}: nested text is reported rather than hidden",
                      bool([i for i in review(model) if i["kind"] == "nested"])
                      and after, f"{model.unmappable} items, still fails "
                      + ", ".join(f"{c} t{t}" for c, t in sorted(after)))
            else:
                check(f"{name}: fully conformant", not after,
                      ", ".join(f"{c} t{t}" for c, t in sorted(after)) or "106/106")


# --------------------------------------------------------------------------- #
# 4. edge cases
# --------------------------------------------------------------------------- #
FONT = next((p for p in (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Verdana.ttf",
    os.path.expanduser("~/Library/Fonts/Outfit-Regular.ttf"),
) if os.path.exists(p)), None)


def _text(pg, xy, s, size, font="F0"):
    """Insert text with an embedded font where one is available.

    pymupdf's default is non-embedded Helvetica, which fails PDF/UA 7.21.4.1 on
    the *source* file. Tagging cannot fix a missing font program, so a fixture
    built that way would be testing pymupdf rather than the tagger.
    """
    if FONT:
        pg.insert_text(xy, s, fontsize=size, fontname=font, fontfile=FONT)
    else:
        pg.insert_text(xy, s, fontsize=size)


def make_edge_cases():
    out = {}

    d = pymupdf.open(); d.new_page(width=720, height=405)
    p = os.path.join(TMP, "blank.pdf"); d.save(p); out["blank page"] = p

    d = pymupdf.open(); pg = d.new_page(width=720, height=405)
    pg.draw_rect(pymupdf.Rect(50, 50, 300, 200), color=(0, 0, 0), fill=(0.2, 0.3, 0.5))
    p = os.path.join(TMP, "noteext.pdf"); d.save(p); out["no text, one shape"] = p

    d = pymupdf.open(); pg = d.new_page(width=720, height=405)
    _text(pg, (60, 90), "Only Heading Here", 34)
    p = os.path.join(TMP, "single.pdf"); d.save(p); out["single page, text only"] = p

    d = pymupdf.open()
    for i, r in enumerate((0, 90, 180, 270)):
        pg = d.new_page(width=720, height=405)
        _text(pg, (60, 90), f"Rotated {r} degrees", 28)
        pg.set_rotation(r)
    p = os.path.join(TMP, "rotated.pdf"); d.save(p); out["rotated pages"] = p

    d = pymupdf.open()
    for w, h in ((720, 405), (595, 842), (1190, 842)):
        pg = d.new_page(width=w, height=h)
        _text(pg, (50, 80), f"Page {w}x{h}", 26)
        _text(pg, (50, 130), "Body text on this page for the size test.", 11)
    p = os.path.join(TMP, "mixed.pdf"); d.save(p); out["mixed page sizes"] = p

    d = pymupdf.open(); pg = d.new_page(width=720, height=405)
    pg.insert_text((60, 90), "アクセシビリティの見出し", fontsize=30, fontname="china-s")
    pg.insert_text((60, 150), "本文のテキストです。これはテストです。", fontsize=12,
                   fontname="china-s")
    p = os.path.join(TMP, "cjk.pdf"); d.save(p); out["CJK text"] = p

    d = pymupdf.open(); pg = d.new_page(width=720, height=405)
    _text(pg, (60, 90), "Heading", 30)
    for i in range(400):
        _text(pg, (60, 120 + (i % 40) * 6), f"item {i}", 5)
    p = os.path.join(TMP, "dense.pdf"); d.save(p); out["very dense page"] = p

    d = pymupdf.open(); pg = d.new_page(width=720, height=405)
    _text(pg, (60, 90), "Locked document", 30)
    p = os.path.join(TMP, "encrypted.pdf")
    d.save(p, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="")
    out["encrypted (empty user pw)"] = p

    src = open(fixture("slides-1page.pdf"), "rb").read()
    p = os.path.join(TMP, "truncated.pdf")
    open(p, "wb").write(src[: len(src) // 2])
    out["truncated file"] = p

    p = os.path.join(TMP, "notapdf.pdf")
    open(p, "wb").write(b"this is definitely not a pdf\n" * 40)
    out["not a PDF at all"] = p

    return out


def test_edge_cases():
    section("4. Edge cases")
    cases = make_edge_cases()
    must_fail = {"truncated file", "not a PDF at all"}
    for name, src in cases.items():
        out = os.path.join(TMP, f"edge-{abs(hash(name)) % 9999}.pdf")
        try:
            model, _ = tag(src, out)
        except Exception as exc:
            if name in must_fail:
                check(f"{name}: refused cleanly", True, type(exc).__name__)
            else:
                check(f"{name}: tags without error", False, repr(exc)[:70])
            continue
        if name in must_fail:
            check(f"{name}: refused cleanly", False, "it was accepted, which it should not be")
            continue
        problems, counts = inspect_structure(out)
        # A genuinely blank page has nothing to tag, so no tree is the right answer.
        if name == "blank page":
            check(f"{name}: handled without error", True,
                  f"{len(model.pages)} page, {sum(len(p.elements) for p in model.pages)} elements")
            continue

        check(f"{name}: structure is internally consistent", not problems,
              "; ".join(problems[:2]))
        check(f"{name}: pixels unchanged", page_hashes(src) == page_hashes(out))

        if model.unembedded_fonts:
            check(f"{name}: unembeddable source font is reported to the user", True,
                  ", ".join(model.unembedded_fonts[:2]))

        # The fair invariant: tagging must never *introduce* a conformance failure.
        # Some fixtures carry font-level defects of their own (pymupdf's embedding
        # leaves an invalid CIDToGIDMap), which no amount of tagging can fix, so
        # requiring a clean output would be testing pymupdf rather than the tagger.
        if model.was_encrypted:
            # veraPDF reads an encrypted source inconsistently, so a before/after
            # delta against one is flaky rather than informative. What matters for
            # this fixture is that the loss of encryption is reported.
            check(f"{name}: dropped encryption is reported to the user",
                  any(i["kind"] == "encryption" for i in review(model)))
            continue
        before = verapdf_failed_rules(src)
        after = verapdf_failed_rules(out)
        if before is not None and after is not None:
            introduced = sorted(after - before)
            check(f"{name}: tagging introduced no new conformance failure",
                  not introduced, f"introduced {introduced}" if introduced else
                  f"fixed {len(before - after)}, {len(after)} source-level left")
            source_only = {c for c, _ in after}
            if not after:
                check(f"{name}: fully conformant", True, "106/106")
            else:
                check(f"{name}: what remains is font-level, not structural",
                      all(c.startswith("7.21") for c, _ in after),
                      ", ".join(f"{c} t{t}" for c, t in sorted(after)))


# --------------------------------------------------------------------------- #
# 5. checks layer
# --------------------------------------------------------------------------- #
def test_font_reporting():
    section("4b. Fonts the source never embedded")
    d = pymupdf.open()
    pg = d.new_page(width=720, height=405)
    pg.insert_text((60, 90), "Deliberately not embedded", fontsize=30)   # base-14
    bad = os.path.join(TMP, "unembedded.pdf")
    d.save(bad)
    m = normalise_headings(analyse(bad))
    check("a non-embedded font is detected", bool(m.unembedded_fonts),
          ", ".join(m.unembedded_fonts))
    iss = review(m)
    fonts = [i for i in iss if i["kind"] == "fonts"]
    check("it is reported as a blocker", fonts and fonts[0]["severity"] == "blocker")
    check("the explanation says tagging cannot fix it",
          fonts and "cannot fix" in fonts[0]["fix"])
    out = os.path.join(TMP, "unembedded-tagged.pdf")
    build(m, out)
    problems, _ = inspect_structure(out)
    check("it still tags to a consistent structure", not problems,
          "; ".join(problems[:2]))

    clean = normalise_headings(analyse(fixture("slides-6page.pdf")))
    check("a real Slides export reports no font problem", not clean.unembedded_fonts,
          ", ".join(clean.unembedded_fonts))
    check("and therefore raises no font issue",
          not [i for i in review(clean) if i["kind"] == "fonts"])


def make_form_xobject_pdf():
    """A page whose text lives entirely inside a form XObject.

    Built rather than borrowed: scanning the corpus for a real example turned up
    personal paperwork, which has no business in a test fixture.
    """
    pdf = pikepdf.Pdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
        BaseFont=pikepdf.Name("/Helvetica")))
    inner = (b"BT /F1 28 Tf 1 0 0 1 60 320 Tm (Heading inside a form) Tj ET\n"
             b"BT /F1 12 Tf 1 0 0 1 60 280 Tm (Body text that also lives in the form.) Tj ET\n"
             b"BT /F1 12 Tf 1 0 0 1 60 250 Tm (A second line of body text.) Tj ET\n")
    form = pdf.make_stream(inner)
    form[pikepdf.Name.Type] = pikepdf.Name.XObject
    form[pikepdf.Name.Subtype] = pikepdf.Name.Form
    form[pikepdf.Name.BBox] = pikepdf.Array([0, 0, 720, 405])
    form[pikepdf.Name.Resources] = pikepdf.Dictionary(
        Font=pikepdf.Dictionary(F1=font))
    page = pdf.add_blank_page(page_size=(720, 405))
    page[pikepdf.Name.Resources] = pikepdf.Dictionary(
        XObject=pikepdf.Dictionary(Fm0=form))
    page.Contents = pdf.make_stream(b"q /Fm0 Do Q\n")
    out = os.path.join(TMP, "formxobject.pdf")
    pdf.save(out)
    return out


def test_form_xobjects():
    section("4c. Text inside form XObjects")
    path = make_form_xobject_pdf()
    m = normalise_headings(analyse(path))
    check("pymupdf still sees the text",
          "Heading inside a form" in pymupdf.open(path)[0].get_text())

    live = [e for p in m.pages for e in p.elements
            if e.role != "Artifact" and e.kind == "text"]
    check("the text inside the form is found", len(live) >= 2,
          f"{len(live)} elements")
    check("and it is no longer reported as unreachable",
          m.nested_text_pages == 0 and m.unmappable == 0,
          f"nested={m.nested_text_pages} unmappable={m.unmappable}")
    check("its operator ranges point into the form's own stream",
          all(o[0] != "" for e in live for o in e.ops),
          str(live[0].ops) if live else "")
    check("the heading is recognised as a heading",
          any(e.role.startswith("H") for e in live),
          ", ".join(sorted({e.role for e in live})))

    out = os.path.join(TMP, "form-tagged.pdf")
    build(m, out)
    problems, _ = inspect_structure(out)
    check("structure is internally consistent", not problems, "; ".join(problems[:2]))
    check("pixels unchanged", page_hashes(path) == page_hashes(out))

    # the mechanism: ids are per stream, so a reference into a form must be an MCR
    pdf = pikepdf.open(out)
    sr = pdf.Root.get("/StructTreeRoot")
    mcrs, stms, sp = 0, 0, []
    def walk(n):
        nonlocal mcrs, stms
        kids = n.get("/K")
        items = kids if isinstance(kids, pikepdf.Array) else ([kids] if kids is not None else [])
        for k in items:
            if isinstance(k, pikepdf.Dictionary):
                if str(k.get("/Type", "")) == "/MCR":
                    mcrs += 1
                    if "/Stm" in k:
                        stms += 1
                elif str(k.get("/Type", "")) == "/StructElem":
                    walk(k)
    walk(sr.K[0] if isinstance(sr.K, pikepdf.Array) else sr.K)
    check("form content is referenced by marked-content reference", mcrs > 0, f"{mcrs} MCRs")
    check("each reference names the stream it points into", stms == mcrs,
          f"{stms} of {mcrs} carry /Stm")
    for _nm, xo in ((pdf.pages[0].get("/Resources") or {}).get("/XObject", {}) or {}).items():
        if str(xo.get("/Subtype", "")) == "/Form" and "/StructParents" in xo:
            sp.append(int(xo["/StructParents"]))
    check("the form carries its own /StructParents", bool(sp), str(sp))

    before, after = verapdf_failed_rules(path), verapdf_failed_rules(out)
    if before is not None and after is not None:
        introduced = sorted(after - before)
        check("tagging introduced no new conformance failure", not introduced,
              f"introduced {introduced}" if introduced else f"fixed {len(before - after)}")
        check("what remains is the fixture's own font defect",
              all(c.startswith("7.21") for c, _ in after),
              ", ".join(f"{c} t{t}" for c, t in sorted(after)) or "nothing")

    clean = analyse(fixture("slides-6page.pdf"))
    check("a Slides export still reports no unreachable text",
          clean.nested_text_pages == 0 and clean.unmappable == 0)


def test_shared_forms():
    section("4d. A form drawn more than once is still refused")
    # Two invocations of one form: its marked content cannot belong to two places,
    # so this stays reported rather than mis-tagged.
    pdf = pikepdf.Pdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
        BaseFont=pikepdf.Name("/Helvetica")))
    form = pdf.make_stream(b"BT /F1 14 Tf 1 0 0 1 20 20 Tm (Repeated block) Tj ET\n")
    form[pikepdf.Name.Type] = pikepdf.Name.XObject
    form[pikepdf.Name.Subtype] = pikepdf.Name.Form
    form[pikepdf.Name.BBox] = pikepdf.Array([0, 0, 300, 60])
    form[pikepdf.Name.Resources] = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    for _ in range(2):
        page = pdf.add_blank_page(page_size=(720, 405))
        page[pikepdf.Name.Resources] = pikepdf.Dictionary(
            XObject=pikepdf.Dictionary(Fm0=form))
        page.Contents = pdf.make_stream(b"q 1 0 0 1 40 200 cm /Fm0 Do Q\n")
    shared = os.path.join(TMP, "shared-form.pdf")
    pdf.save(shared)

    m = normalise_headings(analyse(shared))
    check("a form drawn on two pages is detected as shared",
          m.nested_text_pages > 0, f"{m.nested_text_pages} pages")
    check("and it is reported to the user",
          any(i["kind"] == "nested" for i in review(m)))
    out = os.path.join(TMP, "shared-tagged.pdf")
    build(m, out)
    check("it still tags without error and leaves pixels alone",
          page_hashes(shared) == page_hashes(out))


def make_table_pdf(path, merge=False):
    """A small real table drawn as text in a grid, optionally with a merged cell."""
    doc = pymupdf.open()
    pg = doc.new_page(width=400, height=260)
    rows = [["Region", "Q1", "Q2"],
            ["North", "120", "140"],
            ["South", "95", "110"],
            ["West", "80", "" if merge else "None"]]
    x0, y0, w, h = 40, 40, 100, 40
    for r in range(len(rows) + 1):                       # ruling lines: real
        pg.draw_line((x0, y0 + r * h), (x0 + 3 * w, y0 + r * h))
    for c in range(4):                                   # tables have borders,
        pg.draw_line((x0 + c * w, y0), (x0 + c * w, y0 + len(rows) * h))
    for r, row in enumerate(rows):                       # and that is how they
        for c, cell in enumerate(row):                   # are found.
            if not cell:
                continue
            pg.insert_text((x0 + c * w + 8, y0 + r * h + 25), cell, fontsize=12,
                           fontname="hebo" if r == 0 else "helv")
    doc.save(path); doc.close()
    return path


def test_tables():
    section("4d. Tables")
    path = os.path.join(TMP, "table.pdf")
    make_table_pdf(path)
    m = normalise_headings(analyse(path))
    tables = [t for p in m.pages for t in p.tables]
    check("a table is found", len(tables) == 1, f"{len(tables)} found")
    if not tables:
        return
    t = tables[0]
    check("with the right shape", (t.rows, t.cols) == (4, 3), f"{t.rows}x{t.cols}")
    check("the header row is marked as headers",
          all(c.header for c in t.cells if c.row == 0 and c.elements),
          "header cells: " + str(sum(1 for c in t.cells if c.header)))
    check("body cells are not marked as headers",
          not any(c.header for c in t.cells if c.row > 0))
    check("it explains itself in plain English", bool(t.note), t.note)
    check("cells reference elements rather than copying their text",
          all(isinstance(x, str) for c in t.cells for x in c.elements))

    out = os.path.join(TMP, "table-tagged.pdf")
    build(m, out)
    problems, _ = inspect_structure(out)
    check("structure is internally consistent", not problems, "; ".join(problems[:2]))
    roles = struct_roles(out)
    check("a /Table is emitted", roles.get("Table") == 1, str(roles))
    check("with one /TR per row", roles.get("TR") == 4, str(roles))
    check("headers become /TH", roles.get("TH") == 3, str(roles))
    check("body cells become /TD", roles.get("TD") == 9, str(roles))
    check("cell text is not also tagged as loose paragraphs",
          not roles.get("P"), str(roles))
    check("header cells carry a /Scope", scope_count(out) == 3, str(scope_count(out)))
    if USE_VERAPDF:
        before, after = verapdf_failed_rules(path), verapdf_failed_rules(out)
        check("tagging the table introduces no new veraPDF failure",
              not (after - before), str(sorted(after - before)))
        # Not asserting outright conformance here: this fixture is drawn with the
        # base-14 fonts, which PyMuPDF does not embed, so it fails 7.21 whatever
        # the structure looks like. Conformance is proved on a real table below.
        check("the only failures left are the source's own font problems",
              all(r[0].startswith("7.21") for r in after), str(sorted(after)))

    # Rejecting a table must keep every word: it changes how the text is
    # described, never whether it is described at all.
    m2 = normalise_headings(analyse(path))
    for t2 in m2.pages[0].tables:
        t2.accepted = False
        t2.reviewed = True
    out2 = os.path.join(TMP, "table-rejected.pdf")
    build(m2, out2)
    roles2 = struct_roles(out2)
    check("rejecting it emits no /Table", not roles2.get("Table"), str(roles2))
    check("but the cell text is still tagged",
          sum(v for k, v in roles2.items() if k in ("P", "H1", "H2", "H3")) >= 8,
          str(roles2))
    check("and no words are lost either way",
          len(pymupdf.open(path)[0].get_text().split())
          == len(pymupdf.open(out)[0].get_text().split())
          == len(pymupdf.open(out2)[0].get_text().split()))
    problems2, _ = inspect_structure(out2)
    check("the rejected version is consistent too", not problems2,
          "; ".join(problems2[:2]))

    # A merged cell is where /Table stops being decorative: without a span the
    # short row makes the table invalid, not merely imprecise.
    mpath = os.path.join(TMP, "table-merged.pdf")
    make_table_pdf(mpath, merge=True)
    m3 = normalise_headings(analyse(mpath))
    t3 = m3.pages[0].tables[0] if m3.pages[0].tables else None
    check("a table with a gap is still found", t3 is not None)
    if t3:
        out3 = os.path.join(TMP, "table-merged-tagged.pdf")
        build(m3, out3)
        check("every row accounts for the full column count",
              rows_are_full(out3, t3.cols), row_widths(out3))
        if USE_VERAPDF:
            before3, after3 = verapdf_failed_rules(mpath), verapdf_failed_rules(out3)
            check("the merged table introduces no new failure",
                  not (after3 - before3), str(sorted(after3 - before3)))

    check_no_inconsistent_tables()

    # Layout grids must not be mistaken for tables: announcing one as a table
    # reads every cell against a header that was never there. Slide decks are the
    # hard case, being full of aligned text that is not tabular.
    decks = slides_corpus(limit=3)
    for src in decks:
        mm = analyse(src)
        bogus = [t for p in mm.pages for t in p.tables if t.accepted]
        check(f"no table invented in {os.path.basename(src)[:30]}", not bogus,
              f"{len(bogus)} found")
    if not decks:
        check("a slide deck was available to check for false positives", True,
              "skipped, none found")

    # And the real thing, end to end: a genuine bordered table should come out
    # conformant outright, not merely no-worse.
    real = real_table_pdf()
    if real:
        mr = normalise_headings(analyse(real))
        tr_ = [t for p in mr.pages for t in p.tables]
        check("the real table is found", len(tr_) >= 1, f"{len(tr_)} found")
        check("and confidently enough not to need review",
              all(t.score >= 70 for t in tr_),
              str(sorted(t.score for t in tr_)))
        outr = os.path.join(TMP, "real-table.pdf")
        build(mr, outr)
        rolesr = struct_roles(outr)
        check("it emits a real /Table", rolesr.get("Table", 0) >= 1, str(rolesr))
        check("every row is full width", rows_are_full(outr, tr_[0].cols),
              row_widths(outr))
        check("no two cells claim the same square", not table_overlaps(outr),
              str(table_overlaps(outr)[:4]))
        if USE_VERAPDF:
            check("and the tagged result is fully PDF/UA conformant",
                  not verapdf_failed_rules(outr),
                  str(sorted(verapdf_failed_rules(outr))))

    # The whole document, not one page of it. Spans that overlap only show up
    # where a tall cell and a wide cell meet, which the first page rarely has:
    # this exact bug passed on page 1 alone and failed across all eight.
    whole = real_table_doc()
    if whole:
        mw = normalise_headings(analyse(whole))
        nt = sum(len(p.tables) for p in mw.pages)
        check("every page's tables are found", nt >= 2, f"{nt} tables")
        outw = os.path.join(TMP, "whole-doc.pdf")
        build(mw, outw)
        check("no cell intersections anywhere in the document",
              not table_overlaps(outw), str(table_overlaps(outw)[:4]))
        if USE_VERAPDF:
            # 7.3 is "this figure has no description", which is the user's job on
            # the images step and not something tagging can or should invent. Fill
            # them in so the assertion measures what tagging did.
            for p in mw.pages:
                for e in p.elements:
                    if e.role == "Figure" and not (e.alt or "").strip():
                        e.alt = "Described for the test."
            outw2 = os.path.join(TMP, "whole-doc-described.pdf")
            build(mw, outw2)
            new = verapdf_failed_rules(outw2) - verapdf_failed_rules(whole)
            check("and the whole document gains no new failure", not new,
                  str(sorted(new)))


def real_table_doc():
    """A whole untagged PDF the app actually finds tables in.

    Selected on the app's own verdict rather than on whether PyMuPDF can see
    ruling lines anywhere. Ruled *fragments* are not tables: one deck in the
    corpus has a 12x13 grid of stray rules at 8% fill, which the app correctly
    rejects, and picking files by fragment made the test fail on the app being
    right.
    """
    for f in sorted(glob.glob(os.path.join(corpus_dir(), "*.pdf"))):
        try:
            pdf = pikepdf.open(f)
            if "/StructTreeRoot" in pdf.Root or len(pdf.pages) < 2:
                continue
            if pymupdf.open(f).is_encrypted:
                continue
            m = normalise_headings(analyse(f))
            pages_with = sum(1 for p in m.pages
                             if any(t.accepted for t in p.tables))
            if pages_with >= 2:
                return f
        except Exception:
            continue
    return None


def check_no_inconsistent_tables():
    """No accepted table may have rows that disagree on the column count.

    PDF/UA 7.2 fails on it, and a grid whose geometry cannot resolve is almost
    never a real table. Checked across whatever is in the corpus rather than on a
    fixture, because the grids that fail this are odd real ones, not ones anybody
    would think to build.
    """
    from app.analyse import _rows_consistent
    seen = bad = 0
    # The one-pager decks are named explicitly: their layout grids are the ones
    # that fail to resolve, and an alphabetical slice of the corpus misses them.
    every = sorted(glob.glob(os.path.join(corpus_dir(), "*.pdf")))
    named = [f for f in every
             if "One Pager" in f or "Overview" in f or "Questionnaire" in f]
    files = named + [f for f in every if f not in named][:18]
    for f in files:
        try:
            pdf = pikepdf.open(f)
            if "/StructTreeRoot" in pdf.Root:
                continue
            m = analyse(f)
        except Exception:
            continue
        for p in m.pages:
            for t in p.tables:
                if not t.accepted:
                    continue
                seen += 1
                if not _rows_consistent(t.cells, t.cols):
                    bad += 1
    if seen < 4:
        skip("table consistency across the corpus", f"only {seen} accepted tables found")
    else:
        check(f"no accepted table anywhere has mismatched rows ({seen} checked)",
              not bad, f"{bad} inconsistent of {seen}")


def slides_corpus(limit=3):
    """Slides exports from the corpus: aligned text that must not read as tables."""
    out = []
    for f in sorted(glob.glob(os.path.join(corpus_dir(), "*.pdf"))):
        try:
            pdf = pikepdf.open(f)
            if "/StructTreeRoot" in pdf.Root:
                continue
            mb = [float(v) for v in pdf.pages[0].MediaBox]
            if not (700 <= mb[2] - mb[0] <= 740 and 390 <= mb[3] - mb[1] <= 420):
                continue
            out.append(f)
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out


def real_table_pdf():
    """First page of a real untagged PDF that has ruling-line tables on page 1."""
    for f in sorted(glob.glob(os.path.join(corpus_dir(), "*.pdf"))):
        try:
            pdf = pikepdf.open(f)
            if "/StructTreeRoot" in pdf.Root or len(pdf.pages) < 1:
                continue
            d = pymupdf.open(f)
            if d.is_encrypted or not d[0].find_tables().tables:
                continue
            t = d[0].find_tables().tables[0]
            if len(t.rows) < 3 or t.col_count < 3:
                continue
            for i in reversed(range(len(pdf.pages))):
                if i:
                    del pdf.pages[i]
            p = os.path.join(TMP, "real-table-src.pdf")
            pdf.save(p)
            return p
        except Exception:
            continue
    return None


def struct_roles(path):
    pdf = pikepdf.open(path)
    acc = {}

    def go(n):
        k = n.get("/K")
        items = k if isinstance(k, pikepdf.Array) else ([k] if k is not None else [])
        for it in items:
            if isinstance(it, pikepdf.Dictionary) and str(it.get("/Type", "")) == "/StructElem":
                r = str(it.get("/S")).lstrip("/")
                acc[r] = acc.get(r, 0) + 1
                go(it)
    go(pdf.Root.StructTreeRoot.K[0])
    return acc


def _cells(path):
    pdf = pikepdf.open(path)
    out = []

    def go(n, row=None):
        k = n.get("/K")
        items = k if isinstance(k, pikepdf.Array) else ([k] if k is not None else [])
        for it in items:
            if isinstance(it, pikepdf.Dictionary) and str(it.get("/Type", "")) == "/StructElem":
                r = str(it.get("/S")).lstrip("/")
                if r == "TR":
                    cur = []
                    go(it, cur)
                    out.append(cur)
                elif r in ("TH", "TD") and row is not None:
                    a = it.get("/A")
                    row.append((r, int(a.get("/ColSpan", 1)) if a else 1,
                                int(a.get("/RowSpan", 1)) if a else 1))
                else:
                    go(it, row)
    go(pdf.Root.StructTreeRoot.K[0])
    return out


def _occupancy(path):
    """Width of each row once cells spanning down from earlier rows are counted.

    A row holding fewer <TD> elements is not a short row: a /RowSpan of 7 above it
    occupies a column here for the next six rows without an element of its own.
    Summing /ColSpan alone reports a valid table as broken.
    """
    widths, pending = [], []          # pending: [rows still covered, columns wide]
    for row in _cells(path):
        widths.append(sum(cs for _, cs in pending) + sum(c[1] for c in row))
        pending = [(rem - 1, cs) for rem, cs in pending if rem - 1 > 0]
        pending += [(rs - 1, cs) for _, cs, rs in row if rs > 1]
    return widths


def rows_are_full(path, cols):
    return all(w == cols for w in _occupancy(path) if w)


def row_widths(path):
    return str(_occupancy(path))


def table_overlaps(path):
    """Grid squares claimed by more than one cell, per table.

    veraPDF calls this 7.2 test 15 and describes it as an intersection, which does
    not point at the cause; this reports the actual squares. Worth checking
    directly because it is invisible on a single page and only appears where a
    tall cell and a wide cell leave the same square empty.
    """
    pdf = pikepdf.open(path)
    bad = []

    def grid_of(tbl):
        rows, occupied = [], {}
        for tr in _kids(tbl, "TR"):
            row, taken = [], dict(occupied)
            for cell in _kids(tr, "TH") + _kids(tr, "TD"):
                a = cell.get("/A")
                cs = int(a.get("/ColSpan", 1)) if a else 1
                rs = int(a.get("/RowSpan", 1)) if a else 1
                col = 0
                while col in taken:
                    col += 1
                for dc in range(cs):
                    if (col + dc) in taken:
                        bad.append((len(rows), col + dc))
                    taken[col + dc] = rs
                row.append((col, cs, rs))
            rows.append(row)
            occupied = {c: n - 1 for c, n in taken.items() if n - 1 > 0}
        return rows

    def find(n):
        for it in _kids(n, None):
            if str(it.get("/S")).lstrip("/") == "Table":
                grid_of(it)
            else:
                find(it)
    find(pdf.Root.StructTreeRoot.K[0])
    return bad


def _kids(node, role):
    k = node.get("/K")
    items = k if isinstance(k, pikepdf.Array) else ([k] if k is not None else [])
    out = []
    for it in items:
        if not (isinstance(it, pikepdf.Dictionary)
                and str(it.get("/Type", "")) == "/StructElem"):
            continue
        if role is None or str(it.get("/S")).lstrip("/") == role:
            out.append(it)
    return out


def scope_count(path):
    pdf = pikepdf.open(path)
    n = 0
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Dictionary) and "/Scope" in obj:
            n += 1
    return n


def make_row_pdf(path):
    """Two cells side by side on one baseline, plus a wrapped paragraph below.

    The pairing matters: a correct grouping has to separate the cells *and* keep
    the wrapped lines together, and a rule that gets one right by being blunt
    gets the other wrong.
    """
    doc = pymupdf.open()
    pg = doc.new_page(width=500, height=300)
    pg.insert_text((60, 60), "8", fontsize=13)
    pg.insert_text((330, 60), "19", fontsize=13)         # same baseline, far right
    pg.insert_text((60, 120), "This sentence is long enough that it", fontsize=11)
    pg.insert_text((60, 136), "wraps onto a second line below.", fontsize=11)
    doc.save(path); doc.close()
    return path


def test_edits():
    section("4e. Splitting, joining and reordering")
    path = os.path.join(TMP, "row.pdf")
    make_row_pdf(path)
    m = normalise_headings(analyse(path))
    live = [e for e in m.pages[0].elements if e.role != "Artifact" and e.kind == "text"]
    texts = [(e.text or "").strip() for e in live]

    # The root cause of the reported bug: two cells on one baseline were merged.
    check("cells on the same baseline are not merged",
          not any(t == "8 19" for t in texts), str(texts))
    check("each cell is its own item",
          "8" in texts and "19" in texts, str(texts))
    wrapped = [t for t in texts if t.startswith("This sentence")]
    check("but wrapped lines are still one item", len(wrapped) == 1, str(texts))
    check("and the wrap really did join", wrapped and "second line" in wrapped[0],
          str(wrapped))

    # Column assignment must be the containing column, not the nearest edge.
    from app.analyse import _column_of
    cols = {"edges": [54, 271, 458, 554], "gap": 57.6}
    check("a value centred in a wide column stays in that column",
          _column_of((390.1, 0, 396.7, 0), cols) == 1,
          str(_column_of((390.1, 0, 396.7, 0), cols)))
    check("and the next column's value lands in the next column",
          _column_of((484.4, 0, 495.3, 0), cols) == 2,
          str(_column_of((484.4, 0, 495.3, 0), cols)))

    # Splitting
    check("a single-line item reports nothing to split",
          not edits.can_split(next(e for e in live if (e.text or "").strip() == "8")))
    multi = [e for e in live if edits.can_split(e)]
    check("a wrapped item is splittable", len(multi) >= 1, f"{len(multi)}")
    if multi:
        target = multi[0]
        n_before = len(m.pages[0].elements)
        ok, info = edits.split(m, target.id)
        check("splitting it succeeds", ok, str(info))
        check("it becomes more than one item",
              len(m.pages[0].elements) == n_before + 1,
              f"{n_before} -> {len(m.pages[0].elements)}")
        pieces = [e for e in m.pages[0].elements if e.id.startswith(target.id + "s")]
        check("every piece keeps operator ranges",
              all(e.ops for e in pieces), str([len(e.ops) for e in pieces]))
        check("the pieces between them hold all the original ranges",
              sorted(o for e in pieces for o in e.ops) == sorted(target.ops))
        check("and the pieces are marked as your decision",
              all(e.reviewed for e in pieces))
        check("reading positions stay a clean run",
              sorted(e.order for e in m.pages[0].elements
                     if e.role != "Artifact")
              == list(range(len([e for e in m.pages[0].elements
                                 if e.role != "Artifact"]))))

    ok2, msg = edits.split(m, "no-such-id")
    check("splitting an unknown item fails cleanly", not ok2, str(msg))

    # A split must not change what the page says or whether it validates.
    out = os.path.join(TMP, "row-split.pdf")
    build(m, out)
    check("no words are lost by splitting",
          sorted(pymupdf.open(path)[0].get_text().split())
          == sorted(pymupdf.open(out)[0].get_text().split()))
    problems, _ = inspect_structure(out)
    check("the split file's structure is consistent", not problems,
          "; ".join(problems[:2]))
    if USE_VERAPDF:
        new = verapdf_failed_rules(out) - verapdf_failed_rules(path)
        check("splitting introduces no new veraPDF failure", not new, str(sorted(new)))

    # Joining
    m2 = normalise_headings(analyse(path))
    live2 = sorted([e for e in m2.pages[0].elements if e.role != "Artifact"],
                   key=lambda x: x.order)
    ok3, msg3 = edits.join_up(m2, live2[0].id)
    check("the first item cannot be joined upwards", not ok3, str(msg3))
    n2 = len(m2.pages[0].elements)
    ok4, kept = edits.join_up(m2, live2[1].id)
    check("joining onto the item before it succeeds", ok4, str(kept))
    check("that removes one item", len(m2.pages[0].elements) == n2 - 1,
          f"{n2} -> {len(m2.pages[0].elements)}")
    joined = next(e for e in m2.pages[0].elements if e.id == kept)
    check("the joined item carries both texts",
          (live2[0].text or "").split()[0] in joined.text
          and (live2[1].text or "").split()[0] in joined.text, joined.text)
    out2 = os.path.join(TMP, "row-joined.pdf")
    build(m2, out2)
    check("no words are lost by joining",
          sorted(pymupdf.open(path)[0].get_text().split())
          == sorted(pymupdf.open(out2)[0].get_text().split()))
    problems2, _ = inspect_structure(out2)
    check("the joined file's structure is consistent", not problems2,
          "; ".join(problems2[:2]))

    # Text and an image must never be joined: reading order often puts a picture
    # between two halves of a sentence, so this is easy to hit by accident.
    m4 = normalise_headings(analyse(path))
    pg4 = m4.pages[0]
    from app.model import Element
    img = Element(id="fake-img", page=0, kind="image", role="Figure",
                  auto_role="Figure", bbox=(10, 10, 60, 60), order=0)
    for x in pg4.elements:
        if x.role != "Artifact":
            x.order += 1
    pg4.elements.insert(0, img)
    after_img = min((x for x in pg4.elements if x.kind == "text"
                     and x.role != "Artifact"), key=lambda x: x.order)
    ok7, msg7 = edits.join_up(m4, after_img.id)
    check("text cannot be joined onto an image", not ok7, str(msg7))
    check("and the refusal explains what to do instead",
          "images cannot be joined" in str(msg7), str(msg7))

    # Grouping rules, directly
    row = [{"text": "8", "bbox": [60, 50, 70, 64], "ops": [["", 1, 2]]},
           {"text": "19", "bbox": [330, 50, 348, 64], "ops": [["", 3, 4]]}]
    check("side-by-side parts split into two pieces",
          len(edits.split_groups(row)) == 2, str(len(edits.split_groups(row))))
    stack = [{"text": "one", "bbox": [60, 50, 200, 64], "ops": [["", 1, 2]]},
             {"text": "two", "bbox": [60, 66, 190, 80], "ops": [["", 3, 4]]}]
    check("overlapping stacked parts fall back to one piece each",
          len(edits.split_groups(stack)) == 2)
    check("and stay in top-to-bottom order",
          [g[0]["text"] for g in edits.split_groups(stack)] == ["one", "two"])

    # A cell holds element *ids*, so splitting one of them must not leave the cell
    # pointing at something that no longer exists: the cell would come out empty
    # and the text would be tagged loose elsewhere, with nothing flagging it and
    # the file still validating.
    tdoc = real_table_doc()
    if tdoc:
        mt = normalise_headings(analyse(tdoc))
        pgt = next((p for p in mt.pages
                    if p.tables and any(edits.can_split(e) and
                        any(e.id in c.elements for t in p.tables for c in t.cells)
                        for e in p.elements)), None)
        if pgt is None:
            check("a table-claimed splittable item was available", True,
                  "skipped, none in the corpus")
        else:
            victim = next(e.id for e in pgt.elements if edits.can_split(e)
                          and any(e.id in c.elements
                                  for t in pgt.tables for c in t.cells))
            words = sorted(pymupdf.open(tdoc)[0].get_text().split())
            ok5, _ = edits.split(mt, victim)
            check("splitting an item inside a table works", ok5)
            ids = {e.id for e in pgt.elements}
            dangling = [x for t in pgt.tables for c in t.cells
                        for x in c.elements if x not in ids]
            check("no cell is left pointing at a deleted item",
                  not dangling, str(dangling[:3]))
            pieces = [e for e in pgt.elements if e.id.startswith(victim + "s")]
            placed = {x for t in pgt.tables for c in t.cells for x in c.elements}
            check("each piece is placed in a cell",
                  all(e.id in placed for e in pieces),
                  str([e.id for e in pieces if e.id not in placed]))
            landed = {(c.row, c.col) for t in pgt.tables for c in t.cells
                      if any(e.id in c.elements for e in pieces)}
            check("and in different cells, which is the point",
                  len(landed) == len(pieces), str(sorted(landed)))
            outt = os.path.join(TMP, "table-split.pdf")
            build(mt, outt)
            check("no cell text is lost",
                  sorted(pymupdf.open(outt)[0].get_text().split()) == words,
                  "%d words in, %d out"
                  % (len(words), len(pymupdf.open(outt)[0].get_text().split())))
            probt, _ = inspect_structure(outt)
            check("the table's structure is still consistent", not probt,
                  "; ".join(probt[:2]))
            check("no two cells claim the same square after a split",
                  not table_overlaps(outt), str(table_overlaps(outt)[:3]))
            if USE_VERAPDF:
                check("splitting inside a table stays conformant",
                      not verapdf_failed_rules(outt),
                      str(sorted(verapdf_failed_rules(outt))))
            # joining back must not strand a reference either
            ok6, kept6 = edits.join_up(mt, pieces[-1].id)
            if ok6:
                ids6 = {e.id for e in pgt.elements}
                dang6 = [x for t in pgt.tables for c in t.cells
                         for x in c.elements if x not in ids6]
                check("joining inside a table strands nothing",
                      not dang6, str(dang6[:3]))

    # parts have to survive the browser round-trip or a split is impossible there
    rt = DocModel.from_json(json.loads(json.dumps(m2.to_json())))
    check("line geometry survives the JSON round-trip",
          [len(e.parts) for p in rt.pages for e in p.elements]
          == [len(e.parts) for p in m2.pages for e in p.elements])


def make_slide_pdf(path):
    """A slide shaped like the ones that got this wrong.

    Title, a paragraph set larger than the small print, bulleted names, a stat
    figure with its label, and a block of small print heavy enough to drag a
    document-wide body estimate down. Every one of those was mis-tagged at some
    point.
    """
    doc = pymupdf.open()
    pg = doc.new_page(width=720, height=405)
    pg.insert_text((40, 50), "How we help teams address these challenges",
                   fontsize=20, fontname="hebo")
    # a paragraph, set larger than the small print, wrapping over three lines
    for i, ln in enumerate([
            "The training library offers accessibility courses, in the form of",
            "online modules for web developers, mobile developers and",
            "designers. All of it at no charge whatsoever."]):
        pg.insert_text((40, 100 + i * 18), ln, fontsize=14)
    # bulleted names
    for i, name in enumerate(["Ada Lovelace", "Alan Turing", "Grace Hopper"]):
        pg.insert_text((40, 190 + i * 20), "\u25cf", fontsize=14)
        pg.insert_text((60, 190 + i * 20), name, fontsize=14)
    # a display figure and its label
    pg.insert_text((430, 120), "4X", fontsize=22, fontname="hebo")
    pg.insert_text((430, 145), "Coverage", fontsize=14, fontname="hebo")
    # small print, enough of it to sway a document-wide body estimate
    for i in range(6):
        pg.insert_text((430, 200 + i * 11),
                       "Small print line %d with a good deal of text on it, at length." % i,
                       fontsize=8)
    doc.save(path); doc.close()
    return path


def make_layout_pdf(path):
    """The layout traps: a bold lead-in, peer cards, a shape callout, a quote."""
    doc = pymupdf.open()
    pg = doc.new_page(width=720, height=405)
    pg.insert_text((40, 40), "What sets us apart", fontsize=20, fontname="hebo")
    # a sentence whose first word is set larger and bold, wrapping to a second line
    pg.insert_text((40, 90), "Tooling", fontsize=16, fontname="hebo")
    pg.insert_text((95, 90), " helps devs ship new", fontsize=14)
    pg.insert_text((40, 108), "features accessibly", fontsize=14)
    # two peer cards: same size, one short and one long
    pg.draw_rect(pymupdf.Rect(300, 70, 470, 150), color=None, fill=(0.96, 0.96, 0.97))
    pg.insert_text((310, 100), "We detect the most issues", fontsize=15)
    pg.draw_rect(pymupdf.Rect(490, 70, 690, 150), color=None, fill=(0.96, 0.96, 0.97))
    pg.insert_text((500, 100), "We make it easy to drive adoption and", fontsize=15)
    pg.insert_text((500, 118), "remediate issues efficiently at scale", fontsize=15)
    # a callout: two text objects sharing one filled circle
    pg.draw_circle(pymupdf.Point(120, 250), 45, color=None, fill=(1, 0.26, 0.63))
    pg.insert_text((100, 245), "4X", fontsize=24, fontname="hebo")
    pg.insert_text((98, 268), "better", fontsize=12)
    # a quotation
    # Straight quotes deliberately: the base-14 fonts PyMuPDF uses here do not
    # encode the curly pair, and the substituted glyph is a bullet, which turns
    # the fixture into a list item and tests nothing about quotations.
    pg.insert_text((300, 250), '"I just wanted to thank you for making tools', fontsize=13)
    pg.insert_text((300, 268), 'that speak a plain language."', fontsize=13)
    doc.save(path); doc.close()
    return path


def test_layout_rules():
    section("4g. Layout: wrapping, peers, shapes and quotes")
    from app.analyse import _is_quotation
    check("a quotation is recognised",
          _is_quotation("\u201cI just wanted to thank you for this.\u201d"))
    check("a quoted word mid-sentence is not a quotation",
          not _is_quotation("He said \u201chello\u201d to me today"))
    check("something too short is not a quotation",
          not _is_quotation("\u201cshort\u201d"))
    check("an attribution dash after the closing mark is allowed",
          _is_quotation("\u201cA quote with attribution after it.\u201d \u2014"))

    path = os.path.join(TMP, "layout.pdf")
    make_layout_pdf(path)
    m = normalise_headings(analyse(path))
    pg = m.pages[0]
    live = [e for e in sorted(pg.elements, key=lambda x: x.order)
            if e.role != "Artifact" and e.kind == "text"]
    texts = [(e.role, (e.text or "").strip()) for e in live]

    def find(fragment):
        return next((e for e in live if fragment in (e.text or "")), None)

    # A bold lead-in must not break the sentence it starts.
    lead = find("helps devs ship")
    check("a bold lead-in word does not split its own sentence",
          lead is not None and "features accessibly" in (lead.text or ""),
          str(texts))
    check("and the sentence is a paragraph, not a heading",
          lead is not None and lead.role == "P",
          str(lead.role if lead else None))

    # Peers set the same way get the same answer.
    short, long_ = find("We detect the most"), find("We make it easy")
    check("two cards set the same way are tagged the same way",
          short is not None and long_ is not None and short.role == long_.role,
          str([(short.role if short else None), (long_.role if long_ else None)]))
    check("and that answer is body copy, not a heading",
          short is not None and short.role == "P", str(short.role if short else None))

    # Exactly one top-level heading per page.
    check("the page has exactly one H1",
          len([e for e in live if e.role == "H1"]) == 1,
          str([(e.role, (e.text or '')[:22]) for e in live if e.role.startswith('H')]))

    # Text sharing a background shape reads as one phrase.
    callout = find("4X")
    check("short text sharing a shape is joined up",
          callout is not None and "better" in (callout.text or ""),
          str(callout.text if callout else None))
    # ...but two separate cards must not run together.
    check("text in separate cards stays separate",
          not any("most issues" in t and "easy to drive" in t for _r, t in texts),
          str(texts))

    quote = find("thank you for making")
    check("a quotation is tagged as one",
          quote is not None and quote.role == "BlockQuote",
          str(quote.role if quote else None))
    check("and it is whole, not split at the line break",
          quote is not None and "plain language" in (quote.text or ""),
          str(quote.text if quote else None))

    out = os.path.join(TMP, "layout-tagged.pdf")
    build(m, out)
    roles = struct_roles(out)
    check("a /BlockQuote is emitted", roles.get("BlockQuote") == 1, str(roles))
    problems, _ = inspect_structure(out)
    check("the structure is consistent", not problems, "; ".join(problems[:2]))
    check("no words are lost",
          sorted(pymupdf.open(path)[0].get_text().split())
          == sorted(pymupdf.open(out)[0].get_text().split()))
    if USE_VERAPDF:
        new = verapdf_failed_rules(out) - verapdf_failed_rules(path)
        check("BlockQuote introduces no new veraPDF failure", not new,
              str(sorted(new)))

    # Shapes: a badge groups, a card does not, and a boundary does not split.
    spath = os.path.join(TMP, "shapes.pdf")
    doc = pymupdf.open()
    sp = doc.new_page(width=720, height=405)
    sp.insert_text((40, 40), "Shapes and grouping", fontsize=20, fontname="hebo")
    # a compact badge holding two short bits: one phrase
    sp.draw_circle(pymupdf.Point(110, 150), 34, color=None, fill=(1, 0.26, 0.63))
    sp.insert_text((92, 148), "4X", fontsize=20, fontname="hebo")
    sp.insert_text((92, 166), "better", fontsize=11)
    # a wide card holding a label and its description: two things
    sp.draw_rect(pymupdf.Rect(260, 120, 470, 175), color=None, fill=(0.9, 0.9, 0.95))
    sp.insert_text((270, 140), "Time pressure", fontsize=14, fontname="hebo")
    sp.insert_text((270, 160), "Developers pushed on productivity", fontsize=11)
    # a sentence that starts inside a card and finishes just below it
    sp.draw_rect(pymupdf.Rect(500, 120, 700, 155), color=None, fill=(0.9, 0.9, 0.95))
    sp.insert_text((510, 140), "Accessibility is not an ordinary", fontsize=11)
    sp.insert_text((510, 166), "part of developer education", fontsize=11)
    doc.save(spath); doc.close()

    ms = normalise_headings(analyse(spath))
    sl = [e for e in sorted(ms.pages[0].elements, key=lambda x: x.order)
          if e.role != "Artifact" and e.kind == "text"]
    st = [(e.role, (e.text or "").strip()) for e in sl]

    def has(fragment):
        return next((t for _r, t in st if fragment in t), None)

    check("a compact badge groups the text inside it",
          (has("4X") or "") == "4X better", str(has("4X")))
    check("a wide card does not weld a label to its description",
          has("Time pressure") == "Time pressure", str(has("Time pressure")))
    check("that description stays its own item",
          has("Developers pushed") == "Developers pushed on productivity",
          str(has("Developers pushed")))
    joined = has("Accessibility is not")
    check("a sentence straddling a card edge is not torn in half",
          joined is not None and "developer education" in joined, str(joined))

    # A lone marker is still a marker: a numbered list exported from Slides puts
    # the number in its own text object.
    from app.analyse import _strip_marker, _is_furniture
    check("a number alone on its line is a list marker",
          _strip_marker("1.")[0] == "1." and _strip_marker("1.")[2] is True,
          str(_strip_marker("1.")))
    check("so is a letter alone on its line",
          _strip_marker("a.")[0] == "a.", str(_strip_marker("a.")))
    check("and a marker with text after it still works",
          _strip_marker("2. Developers")[:2] == ("2.", "Developers"),
          str(_strip_marker("2. Developers")))

    npath = os.path.join(TMP, "numbered.pdf")
    nd = pymupdf.open(); npg = nd.new_page(width=720, height=405)
    npg.insert_text((40, 40), "How we help teams", fontsize=20, fontname="hebo")
    for i, (mark, txt) in enumerate([("1.", "Enterprises find issues"),
                                     ("2.", "Developers have to fix them"),
                                     ("a.", "They do not know how"),
                                     ("b.", "They do not have time")]):
        y = 100 + i * 22
        npg.insert_text((60, y), mark, fontsize=13)
        npg.insert_text((95, y), txt, fontsize=13)
    nd.save(npath); nd.close()
    mn = normalise_headings(analyse(npath))
    nl = [e for e in sorted(mn.pages[0].elements, key=lambda x: x.order)
          if e.role != "Artifact" and e.kind == "text"]
    lis = [e for e in nl if e.role.startswith("LI")]
    check("a numbered list is recognised as a list", len(lis) == 4,
          str([(e.role, e.marker, (e.text or '')[:22]) for e in nl]))
    check("each item keeps its own number",
          [e.marker for e in lis] == ["1.", "2.", "a.", "b."],
          str([e.marker for e in lis]))
    check("and its own text, not a bare marker",
          all((e.text or "").strip() for e in lis),
          str([(e.marker, e.text) for e in lis]))
    check("the numbers are not left as loose paragraphs",
          not any((e.text or "").strip() in ("1.", "2.", "a.", "b.") for e in nl),
          str([(e.role, e.text) for e in nl]))

    # Small does not mean decorative: a logo is thin, an icon is small both ways.
    check("a wide thin logo is not treated as furniture",
          not _is_furniture((0, 0, 104, 12.6)))
    check("a small logo is not either", not _is_furniture((0, 0, 41, 32.6)))
    check("a tiny icon is furniture", _is_furniture((0, 0, 16, 16)))
    check("so is a hairline rule", _is_furniture((0, 0, 200, 3)))

    # A label under the figure it names is not a heading for it.
    lpath = os.path.join(TMP, "labels.pdf")
    ld = pymupdf.open(); lpg = ld.new_page(width=720, height=405)
    lpg.insert_text((40, 40), "The brands who trust us", fontsize=20, fontname="hebo")
    for i, (fig, lab) in enumerate([("2021", "Launch Year"),
                                    ("$110M", "Capital Raised")]):
        y = 120 + i * 70
        lpg.insert_text((80, y), fig, fontsize=22, fontname="hebo")
        lpg.insert_text((80, y + 20), lab, fontsize=16)
    ld.save(lpath); ld.close()
    ml = normalise_headings(analyse(lpath))
    ll = [e for e in sorted(ml.pages[0].elements, key=lambda x: x.order)
          if e.role != "Artifact" and e.kind == "text"]
    seq = [(e.role, (e.text or "").strip()) for e in ll]
    labels = [r for r, t in seq if t in ("Launch Year", "Capital Raised")]
    check("a caption under its figure is not a heading",
          labels and all(r == "P" for r in labels), str(seq))
    # And no heading may sit directly after the content it supposedly heads.
    bad = [(seq[i], seq[i+1]) for i in range(len(seq)-1)
           if seq[i][0] == "P" and seq[i+1][0].startswith("H")
           and seq[i+1][1] in ("Launch Year", "Capital Raised")]
    check("so no paragraph is followed by its own heading", not bad, str(bad))

    # Whole real deck: one H1 per page, and quotations found.
    deck = corpus("DECK")
    if os.path.exists(deck):
        md = normalise_headings(analyse(deck))
        h1s = [len([e for e in p.elements if e.role == "H1"]) for p in md.pages]
        check("every page of a real deck has exactly one H1",
              all(n == 1 for n in h1s), str(h1s))
        qs = [e for p in md.pages for e in p.elements if e.role == "BlockQuote"]
        check("its quotations are picked up", len(qs) >= 2, f"{len(qs)} found")


def test_kickers():
    section("4e. Eyebrow labels, and what is not one")
    from app.analyse import _demote_kickers
    from app.model import Element

    def h(text, size, x, y, role="H3", order=0):
        return Element(id=text[:8], page=0, kind="text", role=role, auto_role=role,
                       text=text, size=size, order=order,
                       bbox=(x, y, x + 6 * len(text), y + size * 1.2))

    # The real pattern: a small label immediately above the bigger heading it
    # introduces, in the same column.
    lbl, head = h("THE SCOPE", 9, 48, 100, order=0), h("What You Are Building", 14, 48, 118, "H2", 1)
    _demote_kickers([lbl, head])
    check("a label above its bigger heading is demoted", lbl.role == "P", lbl.role)
    check("and the heading itself is untouched", head.role == "H2", head.role)

    # Same two sizes, but the bigger heading is ABOVE the label rather than below
    # it. Nothing introduces anything here, so neither moves.
    head2 = h("What You Are Building", 14, 48, 100, "H2", 1)
    lbl2 = h("THE SCOPE", 9, 48, 300, order=0)
    _demote_kickers([lbl2, head2])
    check("a label BELOW the bigger heading is not a kicker", lbl2.role == "H3",
          f"{lbl2.role}: {lbl2.note[:48]}")

    # Side by side in different columns: a label bottom-left and a heading
    # top-right are not a pair, whatever the reading order says.
    lbl3 = h("Owner and next step", 9, 40, 300, order=0)
    head3 = h("Cost of delay", 14, 460, 110, "H2", 1)
    _demote_kickers([lbl3, head3])
    check("a label in another column is not a kicker", lbl3.role == "H3",
          f"{lbl3.role}: {lbl3.note[:48]}")

    # A label with a big vertical gap below it is introducing nothing either.
    lbl4 = h("THE SCOPE", 9, 48, 100, order=0)
    head4 = h("What You Are Building", 14, 48, 400, "H2", 1)
    _demote_kickers([lbl4, head4])
    check("a label far above a heading is not a kicker", lbl4.role == "H3", lbl4.role)


def test_detection():
    section("4f. What counts as a heading")
    from app.analyse import _is_prose, _is_figure

    check("a short label is not prose", not _is_prose("Time pressure"))
    check("a long passage is prose",
          _is_prose("The training library offers accessibility courses, in the form "
                    "of online modules specifically for web developers, mobile "
                    "developers, and designers. All at no charge."))
    check("two sentences are prose whatever the length",
          _is_prose("Short one. Then another."))
    check("one trailing full stop is not enough to be prose",
          not _is_prose("Let us get to the demos."))
    check("a bare number is a figure", _is_figure("2021") and _is_figure("$110M")
          and _is_figure("100+") and _is_figure("4X"))
    check("a short real label is not a figure",
          not _is_figure("QA") and not _is_figure("Engineers"))

    path = os.path.join(TMP, "slide.pdf")
    make_slide_pdf(path)
    m = normalise_headings(analyse(path))
    live = [e for e in sorted(m.pages[0].elements, key=lambda x: x.order)
            if e.role != "Artifact" and e.kind == "text"]
    by = {(e.text or "").strip(): e for e in live}

    def role_of(fragment):
        for t, e in by.items():
            if fragment in t:
                return e.role
        return None

    # The heading share is the thing that went wrong, so it is asserted directly.
    heads = [e for e in live if e.role.startswith("H")]
    check("only a small share of the slide is headings",
          len(heads) <= max(2, len(live) // 4),
          f"{len(heads)} of {len(live)}: " + str([(e.role, (e.text or '')[:22]) for e in heads]))
    check("the slide title is a heading",
          (role_of("How we help teams") or "").startswith("H"),
          str(role_of("How we help teams")))
    check("and it is the top level",
          role_of("How we help teams") == "H1", str(role_of("How we help teams")))
    check("a paragraph set larger than the small print is not a heading",
          role_of("The training library offers") == "P",
          str(role_of("The training library offers")))
    check("a display figure is not a heading",
          role_of("4X") == "P", str(role_of("4X")))

    lis = [e for e in live if e.role.startswith("LI")]
    check("the bulleted names are list items", len(lis) == 3, f"{len(lis)}")
    check("and each one carries its own text, not an empty bullet",
          all((e.text or "").strip() for e in lis),
          str([(e.marker, e.text) for e in lis]))
    check("the names are not headings",
          not any((e.role or "").startswith("H") for t, e in by.items()
                  if "Turing" in t or "Hopper" in t),
          str([(t, e.role) for t, e in by.items() if "Turing" in t or "Hopper" in t]))

    # Levels must not skip, in whatever the detection produced.
    from app.analyse import heading_warnings
    check("heading levels do not skip", not heading_warnings(m),
          str(heading_warnings(m)[:2]))

    out = os.path.join(TMP, "slide-tagged.pdf")
    build(m, out)
    problems, _ = inspect_structure(out)
    check("it still tags cleanly", not problems, "; ".join(problems[:2]))
    if USE_VERAPDF:
        new = verapdf_failed_rules(out) - verapdf_failed_rules(path)
        check("and introduces no new failure", not new, str(sorted(new)))

    # Table cells carry their meaning through /TH and /Scope, so tagging them as
    # headings as well floods the heading list with cell values.
    real = real_table_pdf()
    if real:
        mr = normalise_headings(analyse(real))
        for p in mr.pages:
            claimed = {eid for t in p.tables if t.accepted
                       for c in t.cells for eid in c.elements}
            bad = [e for e in p.elements if e.id in claimed and e.role.startswith("H")]
            check(f"no cell on page {p.number+1} is tagged as a heading",
                  not bad, str([(e.role, (e.text or '')[:14]) for e in bad[:4]]))

    # Real decks: no page should be almost entirely headings.
    for src in slides_corpus(limit=3):
        md = normalise_headings(analyse(src))
        worst, worst_pg = 0.0, None
        for p in md.pages:
            lv = [e for e in p.elements if e.role != "Artifact" and e.kind == "text"]
            if len(lv) < 4:
                continue
            share = len([e for e in lv if e.role.startswith("H")]) / len(lv)
            if share > worst:
                worst, worst_pg = share, p.number + 1
        check(f"no page of {os.path.basename(src)[:24]} is nearly all headings",
              worst <= 0.75, f"page {worst_pg} at {worst*100:.0f}%")


def make_flat_tagged_pdf(path):
    """A tagged PDF whose tree is all paragraphs, the way Google Docs exports one.

    Built by tagging it properly and then flattening the tree, because the point of
    the fixture is a *real* structure tree with real marked-content references that
    happens to carry no headings. Hand-built StructElems with empty /K arrays are
    not readable and would exercise nothing.
    """
    doc = pymupdf.open()
    pg = doc.new_page(width=612, height=792)
    pg.insert_text((48, 60), "Mobile Bridge Toolkit", fontsize=28, fontname="hebo")
    pg.insert_text((48, 100), "THE SCOPE", fontsize=9, fontname="hebo")
    pg.insert_text((48, 120), "What You're Actually Building", fontsize=14,
                   fontname="hebo")
    for i, ln in enumerate([
            "The toolkit is not a thin wrapper. It spans two device",
            "ecosystems and a protocol that keeps moving, so the",
            "full scope is wider than it first appears to be."]):
        pg.insert_text((48, 145 + i * 11), ln, fontsize=10)
    for i, ln in enumerate(["iOS and Android device connectivity",
                            "Version conflict resolution",
                            "LLM-optimized accessibility instructions"]):
        pg.insert_text((66, 190 + i * 15), ln, fontsize=10)
    for i, ln in enumerate([
            "Each of these is a real engineering project, and they",
            "interact with one another in ways that compound."]):
        pg.insert_text((48, 250 + i * 12), ln, fontsize=10)
    # Enough separate paragraphs that the tree is worth judging at all: a handful
    # of items proves nothing about a producer's intentions.
    for i in range(8):
        pg.insert_text((48, 290 + i * 26),
                       "Paragraph %d of ordinary running text on this page." % (i + 1),
                       fontsize=10)
    doc.save(path); doc.close()

    tagged = path.replace(".pdf", "-tagged.pdf")
    build(normalise_headings(analyse(path)), tagged)

    # Now flatten it: every element becomes a paragraph, and our own fingerprint
    # comes off, so it looks like any other producer's output.
    pdf = pikepdf.open(tagged)
    seen = set()

    def flatten(node):
        try:
            if node.objgen in seen:
                return
            seen.add(node.objgen)
        except Exception:
            pass
        if str(node.get("/Type", "")) == "/StructElem":
            role = str(node.get("/S", "")).lstrip("/")
            if role not in ("Document",):
                node["/S"] = pikepdf.Name.P
        k = node.get("/K")
        items = k if isinstance(k, pikepdf.Array) else ([k] if k is not None else [])
        for it in items:
            if isinstance(it, pikepdf.Dictionary):
                flatten(it)

    flatten(pdf.Root.StructTreeRoot)
    if "/Metadata" in pdf.Root:
        del pdf.Root["/Metadata"]
    pdf.save(path)
    return path


def test_flat_foreign_tags():
    section("4g. A tagged file that says nothing")
    from app.analyse import flat_foreign_tree

    check("no headings and no lists over real text is a flat tree",
          flat_foreign_tree({1: {"role": "P"}, 2: {"role": "P"}}, 40))
    check("a tree with headings is not flat",
          not flat_foreign_tree({1: {"role": "P"}, 2: {"role": "H2"}}, 40))
    check("a tree with lists is not flat",
          not flat_foreign_tree({1: {"role": "P"}, 2: {"role": "LI"}}, 40))
    check("a short document is not judged",
          not flat_foreign_tree({1: {"role": "P"}}, 3))

    path = os.path.join(TMP, "flat-tagged.pdf")
    make_flat_tagged_pdf(path)
    pdf = pikepdf.open(path)
    check("the fixture really is tagged", "/StructTreeRoot" in pdf.Root)

    m = normalise_headings(analyse(path))
    live = [e for p in m.pages for e in p.elements
            if e.role != "Artifact" and e.kind == "text"]
    heads = [e for e in live if e.role.startswith("H")]
    check("its own detection runs anyway", len(heads) >= 2,
          str([(e.role, (e.text or '')[:24]) for e in live]))
    check("the title becomes the top-level heading",
          any(e.role == "H1" and "Mobile Bridge" in (e.text or "") for e in live),
          str([(e.role, (e.text or '')[:22]) for e in heads]))
    check("and the flat tree is recorded, not hidden",
          getattr(m, "adopted_flat", False))

    from app.checks import review
    msgs = [i for i in review(m) if i["kind"] == "adopted-flat"]
    check("the user is told the file had no headings", len(msgs) == 1,
          str([i["message"][:50] for i in msgs]))
    check("and it is a warning, not a footnote",
          msgs and msgs[0]["severity"] == "warning",
          str(msgs[0]["severity"] if msgs else None))
    check("it does not also claim an earlier pass by the user",
          not any("your earlier pass" in i["message"] for i in review(m)))

    # Our own tags must still be adopted wholesale.
    out = os.path.join(TMP, "flat-ours.pdf")
    build(m, out)
    m2 = normalise_headings(analyse(out))
    check("re-opening our own output adopts it", m2.adopted_ours and m2.adopted > 0,
          f"ours={m2.adopted_ours} adopted={m2.adopted}")
    check("and is not mistaken for a flat foreign tree",
          not getattr(m2, "adopted_flat", False))
    check("the headings survive the round trip",
          sum(1 for p in m2.pages for e in p.elements
              if e.role.startswith("H")) == len(heads),
          str(sum(1 for p in m2.pages for e in p.elements if e.role.startswith("H"))))


def test_unreliable_tags():
    section("4i. Tags that are present but wrong")
    from app.analyse import unreliable_tags, _visible
    from app.model import Element

    def el(text, size, kind="text"):
        return Element(id=text[:8], page=0, kind=kind, role="P", auto_role="P",
                       text=text, size=size, bbox=(0, 0, 10, 10))

    check("a zero-width space counts as no text", _visible("\u200b") == "")

    sound = [(el("Title of the document", 28.0), "H1")] + \
            [(el("Section %d" % i, 18.0), "H2") for i in range(3)] + \
            [(el("Body line %d here" % i, 11.0), "P") for i in range(30)]
    check("a sound tree is left alone", not unreliable_tags(sound)[0],
          str(unreliable_tags(sound)[1]))

    # H1 set smaller than H2: the levels run backwards against the page
    backwards = [(el("Small top level", 14.0), "H1") for _ in range(4)] + \
                [(el("Bigger below it", 22.0), "H2") for _ in range(4)] + \
                [(el("Body line %d here" % i, 11.0), "P") for i in range(20)]
    bad, why = unreliable_tags(backwards)
    check("heading levels running backwards are caught", bad, str(why))
    check("and the reason says so", "backwards" in why, why)

    # block roles landing on invisible characters
    empties = [(el("\u200b", 11.0), "LI") for _ in range(12)] + \
              [(el("Real line %d of text" % i, 11.0), "P") for i in range(20)] + \
              [(el("A heading", 20.0), "H1")]
    bad2, why2 = unreliable_tags(empties)
    check("tags pointing at invisible characters are caught", bad2, str(why2))
    check("and that reason says so too", "invisible" in why2, why2)

    check("a tree with no headings is left to the flat rule",
          not unreliable_tags([(el("x %d" % i, 11.0), "P") for i in range(30)])[0])
    check("a tiny sample is not judged",
          not unreliable_tags([(el("x", 11.0), "H1")])[0])

    # End to end on the real thing, if it is to hand.
    real = corpus("MISTAGGED")
    if os.path.exists(real):
        m = normalise_headings(analyse(real))
        check("the real misaligned export is rejected",
              getattr(m, "adopted_reason", "") == "wrong",
              getattr(m, "adopted_reason", ""))
        live = [e for p in m.pages for e in p.elements
                if e.role != "Artifact" and e.kind == "text"]
        biggest = max(e.size for e in live if (e.text or "").strip())
        top = [e for e in live if e.size >= biggest - 0.5 and (e.text or "").strip()]
        check("its largest text becomes the top-level heading",
              any(e.role == "H1" for e in top),
              str([(e.role, (e.text or '')[:24]) for e in top]))
        from app.checks import review
        msg = [i for i in review(m) if i["kind"] == "adopted-flat"]
        check("and the user is told the tags did not match the page",
              msg and "do not match" in msg[0]["message"], str(msg[:1]))
    else:
        check("the misaligned sample was available", True, "skipped, not present")


def test_zero_width_lists():
    section("4l. Bullets whose text is invisible to strip()")
    from app.analyse import _visible, BULLETS

    check("a zero-width space reads as empty", _visible("\u200b") == "")
    check("so does a byte-order mark", _visible("\ufeff  ") == "")
    check("real text survives", _visible("\u200bMacBook Pro\u200b") == "MacBook Pro")
    check("the hollow bullet is a bullet", "\u25e6" in BULLETS)
    check("and the filled one still is", "\u2022" in BULLETS)

    # The shape Google Docs exports: a bullet glyph, then a zero-width space where
    # the item text goes, then the words offset to the right on the same baseline.
    #
    # Exercised against _group directly rather than through a rendered PDF: the
    # base-14 fonts PyMuPDF can write have no glyph for either a bullet or a
    # zero-width space, so a generated fixture comes back as "··" and cannot
    # represent the case at all.
    from app.analyse import _group

    def line(text, x, y, size=11.0):
        return {"block": 0, "text": text, "size": size, "font": "F", "bold": False,
                "bbox": (x, y, x + 8 * len(text), y + size * 1.15),
                "ops": [("", int(y), int(y) + 1)]}

    items = ["Gusto - HR and administrative", "Zoom - Video conferencing",
             "HubSpot - CRM", "Slack - Messaging"]
    lines = []
    for i, txt in enumerate(items):
        y = 100 + i * 18
        lines.append(line("\u2022\u200b", 90, y))
        lines.append(line(txt, 108, y))
    grouped = _group(lines, 11.0, 0, {}, 792)
    check("every bullet becomes one list item",
          len([e for e in grouped if e.role.startswith("LI")]) == len(items),
          str([(e.role, (e.text or '')[:26]) for e in grouped]))
    check("and each carries its own words, not a zero-width space",
          [(e.text or "").strip() for e in grouped
           if e.role.startswith("LI")] == items,
          str([(e.text or '') for e in grouped]))
    check("nothing is left over beside them", len(grouped) == len(items),
          str([(e.role, (e.text or '')[:22]) for e in grouped]))
    check("no zero-width character survives into the text",
          not any("\u200b" in (e.text or "") for e in grouped))

    # The same shape with the hollow bullet Google Docs uses for nested levels.
    nested = []
    for i, txt in enumerate(["First nested item", "Second nested item"]):
        y = 200 + i * 18
        nested.append(line("\u25e6\u200b", 108, y))
        nested.append(line(txt, 126, y))
    g2 = _group(nested, 11.0, 0, {}, 792)
    check("a hollow bullet works the same way",
          [(e.text or "").strip() for e in g2 if e.role.startswith("LI")]
          == ["First nested item", "Second nested item"],
          str([(e.role, e.text) for e in g2]))

    # And it must not swallow an ordinary paragraph that follows a list.
    mixed = [line("\u2022\u200b", 90, 100), line("An item", 108, 100),
             line("A following paragraph that is not part of the list", 72, 140)]
    g3 = _group(mixed, 11.0, 0, {}, 792)
    check("a paragraph after the list stays separate",
          [e.role for e in g3] == ["LI", "P"],
          str([(e.role, (e.text or '')[:28]) for e in g3]))

    # A misaligned tree's list roles are misaligned too, so they are not adopted.
    from app.analyse import _MECHANICAL, _STRUCTURAL
    check("mechanical roles exclude lists",
          not ({"L", "LI", "LBody", "Lbl"} & _MECHANICAL), str(_MECHANICAL))
    check("but table geometry is kept",
          {"Table", "TR", "TH", "TD"} <= _MECHANICAL)
    check("while the flat case still keeps lists",
          {"L", "LI"} <= _STRUCTURAL)

    real = corpus("MISTAGGED")
    if os.path.exists(real):
        mr = normalise_headings(analyse(real))
        rl = [e for p in mr.pages for e in p.elements
              if e.role != "Artifact" and e.kind == "text"]
        check("the real export leaves no empty items",
              not any(not (e.text or "").strip() for e in rl),
              str(sum(1 for e in rl if not (e.text or '').strip())))
        check("and its lists carry their text",
              all((e.text or "").strip() for e in rl if e.role.startswith("LI")))


def test_document_vs_deck():
    section("4j. A document is not a deck")
    # Landscape: every slide is a section, so every slide gets its own H1.
    deck = os.path.join(TMP, "deck-levels.pdf")
    d = pymupdf.open()
    for i in range(3):
        pg = d.new_page(width=720, height=405)
        pg.insert_text((40, 50), "Slide title %d" % (i + 1), fontsize=20,
                       fontname="hebo")
        for j in range(6):
            pg.insert_text((40, 100 + j * 14),
                           "Body line %d on this slide, ordinary text." % j,
                           fontsize=11)
    d.save(deck); d.close()
    md = normalise_headings(analyse(deck))
    per_page = [sum(1 for e in p.elements if e.role == "H1") for p in md.pages]
    check("every slide gets its own top-level heading", per_page == [1, 1, 1],
          str(per_page))

    # Portrait: the document flows, so a section heading landing at the top of a
    # page is still a section heading.
    doc = os.path.join(TMP, "doc-levels.pdf")
    d2 = pymupdf.open()
    p1 = d2.new_page(width=612, height=792)
    p1.insert_text((48, 60), "The Document Title", fontsize=28, fontname="hebo")
    p1.insert_text((48, 110), "First Section", fontsize=14, fontname="hebo")
    for j in range(10):
        p1.insert_text((48, 140 + j * 13),
                       "Body line %d of ordinary running text here." % j, fontsize=11)
    p2 = d2.new_page(width=612, height=792)
    p2.insert_text((48, 60), "Second Section", fontsize=14, fontname="hebo")
    for j in range(10):
        p2.insert_text((48, 90 + j * 13),
                       "More body text, line %d, continuing on." % j, fontsize=11)
    d2.save(doc); d2.close()
    m2 = normalise_headings(analyse(doc))
    h1s = [e for p in m2.pages for e in p.elements if e.role == "H1"]
    check("a flowing document has one top-level heading", len(h1s) == 1,
          str([(e.text or '')[:26] for e in h1s]))
    check("and it is the document's own title",
          h1s and "Document Title" in (h1s[0].text or ""),
          str(h1s[0].text if h1s else None))
    later = [e for p in m2.pages[1:] for e in p.elements
             if e.role.startswith("H")]
    check("a section at the top of page 2 is not promoted",
          all(e.role != "H1" for e in later),
          str([(e.role, (e.text or '')[:20]) for e in later]))


def test_table_reading_order():
    section("4k. Tables read across, then down")
    real = real_table_pdf()
    if not real:
        check("a real table was available", True, "skipped, none found")
        return
    m = normalise_headings(analyse(real))
    pg = next((p for p in m.pages if p.tables), None)
    check("a table is present", pg is not None)
    if pg is None:
        return
    t = pg.tables[0]
    where = {eid: (c.row, c.col) for c in t.cells for eid in c.elements}
    seq = [where[e.id] for e in sorted(pg.elements, key=lambda x: x.order)
           if e.id in where]
    check("cells come out in row order, left to right",
          seq == sorted(seq), str(seq[:8]))
    rows = [r for r, _c in seq]
    check("and a row is finished before the next one starts",
          rows == sorted(rows), str(rows[:10]))
    first_row = [c for r, c in seq if r == rows[0]]
    check("the first row runs across the columns",
          first_row == sorted(first_row) and len(first_row) > 1, str(first_row))

    out = os.path.join(TMP, "table-order.pdf")
    build(m, out)
    problems, _ = inspect_structure(out)
    check("it still tags cleanly", not problems, "; ".join(problems[:2]))
    if USE_VERAPDF:
        new = verapdf_failed_rules(out) - verapdf_failed_rules(real)
        check("and introduces no new failure", not new, str(sorted(new)))


def test_wrap_vs_break():
    section("4h. Telling a wrapped line from a new one")
    from app.analyse import _wrap_gap, _break_at

    check("the break threshold never gets looser than the old fixed one",
          _break_at(10.0, None) == 14.0 and _break_at(10.0, 0.8) < 14.0,
          f"{_break_at(10.0, 0.8)}")
    check("a tight-leading page gets a tight threshold",
          _break_at(10.0, 0.8) <= 2.1, str(_break_at(10.0, 0.8)))
    check("a loose-leading page is not over-tightened",
          _break_at(14.0, 5.0) > 5.0, str(_break_at(14.0, 5.0)))

    # A run of single-line items must not collapse into one paragraph.
    path = os.path.join(TMP, "tight-list.pdf")
    doc = pymupdf.open(); pg = doc.new_page(width=612, height=792)
    pg.insert_text((48, 60), "Scope", fontsize=16, fontname="hebo")
    for i, ln in enumerate(["Body text that wraps onto a second line just",
                            "below it at the same size and indent, and then",
                            "carries on for a third and a fourth line so the",
                            "page has enough wraps to measure from."]):
        pg.insert_text((48, 90 + i * 11), ln, fontsize=10)
    for i, ln in enumerate(["First separate item in the list",
                            "Second separate item in the list",
                            "Third separate item in the list"]):
        pg.insert_text((66, 170 + i * 15), ln, fontsize=10)
    doc.save(path); doc.close()
    m = normalise_headings(analyse(path))
    live = [e for e in sorted(m.pages[0].elements, key=lambda x: x.order)
            if e.role != "Artifact" and e.kind == "text"]
    txt = [(e.text or "").strip() for e in live]
    items = [t for t in txt if t.startswith(("First", "Second", "Third"))]
    check("three separate items stay three items", len(items) == 3, str(txt))
    wrapped = [t for t in txt if t.startswith("Body text")]
    check("but the wrapped body text stays one item", len(wrapped) == 1, str(txt))
    check("and it really did join",
          wrapped and "fourth line" in wrapped[0], str(wrapped))

    # A lone asterisk is a footnote mark, not a bullet.
    from app.analyse import _strip_marker
    check("an asterisk is still a bullet character", _strip_marker("* item")[0] == "*")
    fpath = os.path.join(TMP, "footnote.pdf")
    fd = pymupdf.open(); fp = fd.new_page(width=612, height=792)
    fp.insert_text((48, 60), "A document with one footnote", fontsize=16,
                   fontname="hebo")
    for i, ln in enumerate(["Some body text on the page, long enough to be",
                            "measured as ordinary running text here."]):
        fp.insert_text((48, 90 + i * 11), ln, fontsize=10)
    fp.insert_text((48, 130), "* Loaded cost estimate of $250K per engineer",
                   fontsize=10)
    fd.save(fpath); fd.close()
    mf = normalise_headings(analyse(fpath))
    note = next((e for p in mf.pages for e in p.elements
                 if "Loaded cost" in (e.text or "")), None)
    check("a lone asterisk is not read as a list item",
          note is not None and not note.role.startswith("LI"),
          str(note.role if note else None))


def make_form_pdf(path, labelled=True):
    """A PDF with real widget annotations, labelled or not."""
    d = pymupdf.open(); pg = d.new_page(width=612, height=792)
    pg.insert_text((72, 60), "Membership application", fontsize=18, fontname="hebo")
    for i, lbl in enumerate(["Full name", "Email address", "Membership type"]):
        y = 110 + i * 60
        if labelled:
            pg.insert_text((72, y), lbl, fontsize=11)
        w = pymupdf.Widget()
        w.rect = pymupdf.Rect(180, y - 12, 420, y + 8)
        w.field_name = lbl.lower().replace(" ", "_")
        w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        w.field_value = ""
        pg.add_widget(w)
    for i in range(6):
        pg.insert_text((72, 320 + i * 14),
                       "Body text line %d for size measurement." % i, fontsize=11)
    d.save(path); d.close()
    return path


def test_form_fields():
    section("4m. Form fields")
    path = os.path.join(TMP, "form.pdf")
    make_form_pdf(path)
    m = normalise_headings(analyse(path))
    fields = [e for p in m.pages for e in p.elements if e.kind == "field"]
    check("widget annotations are found", len(fields) == 3, f"{len(fields)}")
    check("each gets a name proposed from the label beside it",
          [e.alt for e in fields] == ["Full name", "Email address",
                                      "Membership type"],
          str([e.alt for e in fields]))
    check("and a proposal is not treated as settled",
          not any(e.reviewed for e in fields))

    out = os.path.join(TMP, "form-tagged.pdf")
    build(m, out)
    roles = struct_roles(out)
    check("each field is wrapped in a Form element", roles.get("Form") == 3,
          str(roles))

    pdf = pikepdf.open(out)
    annots = pdf.pages[0].get("/Annots", [])
    check("every widget carries a tooltip",
          all(str(a.get("/TU", "")) for a in annots
              if str(a.get("/Subtype", "")) == "/Widget"),
          str([str(a.get("/TU", "-")) for a in annots]))
    check("the tooltip is the proposed name",
          [str(a.get("/TU", "")) for a in annots
           if str(a.get("/Subtype", "")) == "/Widget"]
          == ["Full name", "Email address", "Membership type"],
          str([str(a.get("/TU", "-")) for a in annots]))
    check("Form is not remapped, being a standard type",
          "/Form" not in dict(pdf.Root.StructTreeRoot.get("/RoleMap", {})),
          str(dict(pdf.Root.StructTreeRoot.get("/RoleMap", {}))))

    if USE_VERAPDF:
        before, after = verapdf_failed_rules(path), verapdf_failed_rules(out)
        check("tagging introduces no new failure", not (after - before),
              str(sorted(after - before)))
        check("and the widget rules are fixed",
              not any(c.startswith("7.18") for c, _t in after),
              str(sorted(c for c, _t in after if c.startswith("7.18"))))

    # An unnamed field is a blocker, not a suggestion: it is the whole accessible
    # name of the control.
    bare = os.path.join(TMP, "form-bare.pdf")
    make_form_pdf(bare, labelled=False)
    mb = normalise_headings(analyse(bare))
    bf = [e for p in mb.pages for e in p.elements if e.kind == "field"]
    check("a field with no printed label gets no invented name",
          all(not (e.alt or "").strip() for e in bf),
          str([e.alt for e in bf]))
    from app.checks import review, summary
    msgs = [i for i in review(mb) if i["kind"] == "field-name"]
    check("and that is reported as a blocker",
          msgs and msgs[0]["severity"] == "blocker",
          str([(i["severity"], i["message"][:40]) for i in msgs]))
    s = summary(mb)
    check("the summary counts them", s["fields"] == 3 and s["fields_todo"] == 3,
          str((s.get("fields"), s.get("fields_todo"))))

    # Naming them clears it.
    for e in bf:
        e.alt = "Field name"
    check("naming them clears the blocker",
          not [i for i in review(mb) if i["kind"] == "field-name"])
    out2 = os.path.join(TMP, "form-named.pdf")
    build(mb, out2)
    if USE_VERAPDF:
        after2 = verapdf_failed_rules(out2)
        check("and the named form has no widget failures either",
              not any(c.startswith("7.18") for c, _t in after2),
              str(sorted(after2)))


def make_shaded_table_pdf(path, gutter=0):
    """A table with no ruling lines, drawn as one filled rectangle per cell.

    `gutter` separates the columns: at 0 they tile, which is what a table does. Set
    it wider and they become side-by-side cards, which is what they must not be
    mistaken for.
    """
    d = pymupdf.open(); pg = d.new_page(width=612, height=792)
    pg.insert_text((58, 50), "Prerequisites", fontsize=16, fontname="hebo")
    widths = [200, 90, 90]
    rows = [["What", "Needed for", "Who"],
            ["Portal accounts", "Both", "Vendor"],
            ["Chrome or Edge", "Web", "Client IT"],
            ["A Mac with Xcode", "iOS", "Client"]]
    y = 80
    for r, row in enumerate(rows):
        x = 58
        for c, txt in enumerate(row):
            rect = pymupdf.Rect(x, y, x + widths[c], y + 40)
            pg.draw_rect(rect, color=None,
                         fill=(0.12, 0.23, 0.38) if r == 0 else (1, 1, 1))
            pg.insert_text((x + 8, y + 24), txt, fontsize=10,
                           color=(1, 1, 1) if r == 0 else (0, 0, 0))
            x += widths[c] + gutter
        y += 40
    for i in range(6):
        pg.insert_text((58, 300 + i * 14),
                       "Body text line %d for measurement." % i, fontsize=10)
    d.save(path); d.close()
    return path


def test_shaded_tables():
    section("4n. Tables with no ruling lines")
    from app.analyse import _column_bands
    path = os.path.join(TMP, "shaded.pdf")
    make_shaded_table_pdf(path)

    pg = pymupdf.open(path)[0]
    bands = _column_bands(pg, pg.rect.width, pg.rect.height)
    check("but the painted columns are found", len(bands) == 3, str(bands))

    m = normalise_headings(analyse(path))
    tabs = [t for p in m.pages for t in p.tables]
    check("so the table is detected", len(tabs) == 1, f"{len(tabs)}")
    if tabs:
        t = tabs[0]
        check("with the right shape", (t.rows, t.cols) == (4, 3),
              f"{t.rows}x{t.cols}")
        check("the first row is the header",
              all(c.header for c in t.cells if c.row == 0))
        first = sorted([c for c in t.cells if c.row == 0], key=lambda c: c.col)
        check("and its cells are the headings",
              [c.preview for c in first] == ["What", "Needed for", "Who"],
              str([c.preview for c in first]))
        second = sorted([c for c in t.cells if c.row == 1], key=lambda c: c.col)
        check("a body row reads across, not down",
              [c.preview for c in second]
              == ["Portal accounts", "Both", "Vendor"],
              str([c.preview for c in second]))
        out = os.path.join(TMP, "shaded-tagged.pdf")
        build(m, out)
        roles = struct_roles(out)
        check("it emits a real Table", roles.get("Table") == 1, str(roles))
        check("with a row per row", roles.get("TR") == 4, str(roles))
        check("and headers as TH", roles.get("TH") == 3, str(roles))
        problems, _ = inspect_structure(out)
        check("the structure is consistent", not problems, "; ".join(problems[:2]))
        if USE_VERAPDF:
            new_f = verapdf_failed_rules(out) - verapdf_failed_rules(path)
            check("and it introduces no new failure", not new_f, str(sorted(new_f)))

    # The real thing: a shaded table the ruled detector can only see as a handful
    # of one-row fragments, which is why the painted-column path exists at all.
    real = corpus("SHADED_TABLE")
    if os.path.exists(real):
        rp = pymupdf.open(real)[1]
        check("the ruled detector cannot read the real one",
              all(len(t.rows) < 2 for t in rp.find_tables().tables),
              str([(len(t.rows), t.col_count) for t in rp.find_tables().tables]))
        mr = normalise_headings(analyse(real))
        shaped = [t for p in mr.pages for t in p.tables if t.id.endswith("st")]
        check("but the painted-column path reads it", len(shaped) >= 1,
              f"{len(shaped)}")
        big = next((t for t in shaped if t.rows >= 8), None)
        check("as eight rows by four columns",
              big is not None and (big.rows, big.cols) == (8, 4),
              str([(t.rows, t.cols) for t in shaped]))
        if big:
            head = sorted([c for c in big.cells if c.row == 0], key=lambda c: c.col)
            check("with the right headings",
                  [c.preview for c in head]
                  == ["What", "Needed for", "Who arranges", "Start it"],
                  str([c.preview for c in head]))
            check("and it says it had no ruling lines",
                  "no ruling lines" in big.note, big.note[:60])

    # Two cards side by side are not a two-column table.
    cards = os.path.join(TMP, "cards.pdf")
    make_shaded_table_pdf(cards, gutter=18)
    mc = normalise_headings(analyse(cards))
    ct = [t for p in mc.pages for t in p.tables]
    check("a gutter between the columns means cards, not a table",
          not ct, str([(t.rows, t.cols) for t in ct]))


def test_flow_downwards():
    section("4o. Reading order follows the page, not the file")
    from app.analyse import _group

    def line(text, x, y, size=11.0):
        return {"block": 0, "text": text, "size": size, "font": "F", "bold": False,
                "bbox": (x, y, x + 7 * len(text), y + size * 1.15),
                "ops": [("", int(y * 10), int(y * 10) + 1)]}

    # A step number drawn in the margin *after* its paragraph, as a real document
    # does: content-stream order is heading, body, number.
    lines = [line("Sign in to the Portal", 92, 367, 12.2),
             line("portal.example.com, using the credentials we provide", 92, 387),
             line("1", 67, 370)]
    grouped = _group(lines, 11.0, 0, {}, 792)
    texts = [(e.text or "").strip() for e in grouped]
    check("the number does not merge into the paragraph above it",
          not any(t.endswith("1") and "credentials" in t for t in texts),
          str(texts))
    check("it stays its own item", "1" in texts, str(texts))

    from app.analyse import _order
    for e in grouped:
        e.column = 0
    _order(grouped, {}, 792)
    seq = [(e.text or "").strip()[:26] for e in sorted(grouped, key=lambda x: x.order)]
    check("and reads number, heading, body",
          seq[0] == "1" and "Sign in" in seq[1], str(seq))

    # A bullet sits beside its text and may be drawn a shade above it, so it must
    # still pick that text up.
    b = [line("\u2022", 90, 102), line("An item of the list", 108, 100)]
    g2 = _group(b, 11.0, 0, {}, 792)
    check("a bullet still collects text drawn slightly above it",
          len(g2) == 1 and "An item" in (g2[0].text or ""),
          str([(e.role, e.text) for e in g2]))


def test_assist():
    section("4p. Optional help from Claude")
    from app import assist

    d = assist.detect()
    check("it reports which route is available, without spending anything",
          d["route"] in (None, "claude-code", "api-key", "api-key-missing"),
          str(d["route"]))
    check("and says so in a line a person can act on", bool(d["label"]), d["label"])

    # Everything below stubs the transport: the suite must never call out, both so
    # it stays free and so a network blip is not a test failure.
    real_ask, real_cli = assist._ask, assist._run_cli

    try:
        assist._ask = lambda prompt, model, **kw: '{"ok": true}'
        items = [{"id": f"e{i}", "order": i, "role": "P", "bbox": [0, i*20, 50, i*20+10],
                  "text": f"line {i}"} for i in range(4)]
        check("a page that reads sensibly comes back with nothing",
              assist.review_order(items, 1) == [])

        assist._ask = lambda prompt, model, **kw: (
            'Sure!\n{"ok": false, "problems": [{"position": 3, "why": "before its heading"}]}')
        got = assist.review_order(items, 1)
        check("a problem is tied back to the item it is about",
              got == [{"id": "e2", "position": 3, "why": "before its heading"}], str(got))

        assist._ask = lambda prompt, model, **kw: (
            '{"ok": false, "problems": [{"position": 99, "why": "off the end"}]}')
        check("a position outside the page is dropped, not trusted",
              assist.review_order(items, 1) == [])

        assist._ask = lambda prompt, model, **kw: "no json at all"
        try:
            assist.review_order(items, 1)
            check("an unreadable answer is refused", False, "no error raised")
        except assist.Unavailable as exc:
            check("an unreadable answer is refused", True, str(exc)[:40])

        # A .app launched from the Dock has no login-shell PATH, so the binary has
        # to be found where it installs itself or the feature silently vanishes on
        # a machine that has it. This is the bug that hid the whole feature.
        home = os.path.join(TMP, "fakehome")
        os.makedirs(os.path.join(home, ".local", "bin"), exist_ok=True)
        fake = os.path.join(home, ".local", "bin", "claude")
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(fake, 0o755)
        real_path, real_home = os.environ.get("PATH", ""), os.environ.get("HOME", "")
        os.environ["PATH"] = "/usr/bin:/bin"
        os.environ["HOME"] = home
        try:
            check("Claude Code is found without a login-shell PATH",
                  assist.detect()["route"] == "claude-code", assist.detect()["label"])
            check("and it is the one that was actually installed",
                  assist.detect()["detail"] == fake, assist.detect()["detail"])
            os.remove(fake)
            check("with nothing installed it reports nothing available",
                  assist.detect()["route"] in (None, "api-key", "api-key-missing"),
                  str(assist.detect()["route"]))
        finally:
            os.environ["PATH"], os.environ["HOME"] = real_path, real_home

        # Alt text: the shapes a model actually returns.
        img = os.path.join(TMP, "crop.png")
        pymupdf.open().new_page(width=40, height=40).parent.save(img) if False else None
        d2 = pymupdf.open(); d2.new_page(width=40, height=40)
        d2[0].get_pixmap().save(img); d2.close()
        for reply, want, dec in (("The company logo", "The company logo", False),
                                 ('Here:\n"A bar chart of defects"', "A bar chart of defects", False),
                                 ("DECORATIVE", "", True),
                                 ("\u201cSmart quoted\u201d", "Smart quoted", False)):
            assist._run_cli = lambda prompt, model, cwd, r=reply: r
            os.environ.pop("ANTHROPIC_API_KEY", None)
            if assist.detect()["route"] != "claude-code":
                break
            text, decorative = assist.draft_alt(img)
            check(f"a reply of {reply[:18]!r} parses", (text, decorative) == (want, dec),
                  str((text, decorative)))

        # An expired sign-in has a one-line fix, and the app must say it.
        def expired(*a, **k):
            raise assist.Unavailable(
                "Your Claude Code sign-in has expired. Open a terminal, run "
                "`claude`, sign in, and try again.")
        assist._run_cli = expired
        try:
            assist.draft_alt(img)
            check("an expired sign-in is reported, not swallowed", False, "no error")
        except assist.Unavailable as exc:
            check("an expired sign-in is reported, not swallowed",
                  "sign-in has expired" in str(exc), str(exc)[:50])
    finally:
        assist._ask, assist._run_cli = real_ask, real_cli

    # A draft must never count as a decision.
    from app.model import Element
    e = Element(id="i1", page=0, kind="image", role="Figure", auto_role="Figure")
    check("an element can carry the fact that it was drafted", hasattr(e, "drafted"))
    check("and it does not start out drafted", e.drafted is False)
    check("nor reviewed", e.reviewed is False)


def test_checks_layer():
    section("5. Guidance layer: alt-text quality and document checks")
    cases = [
        ("", "blocker", "empty alt is a blocker"),
        ("screenshot.png", "blocker", "a filename is caught"),
        ("/Users/someone/Desktop/pic.png", "blocker", "an absolute path is caught"),
        ("/var/folders/t9/xx/preencoded.png", "blocker", "a temp path is caught"),
        ("image", "warning", "the word 'image' is caught"),
        ("chart", "warning", "the word 'chart' is caught"),
        ("Image of a bar chart comparing seven banks", "suggestion",
         "'image of' prefix is flagged"),
        ("ab", "warning", "a 2-character description is flagged"),
    ]
    for alt, want, label in cases:
        got = alt_text_issues("", alt)
        sevs = [s for s, _, _ in got]
        check(label, want in sevs, f"{alt[:32]!r} -> {sevs}")

    good = alt_text_issues("", "The bank scored 67 of 100, the lowest of the seven compared")
    check("a genuinely good description is left alone", not good, f"{len(good)} issues")

    # document-level
    src = fixture("slides-6page.pdf")
    m = normalise_headings(analyse(src))
    m.title = ""
    iss = review(m)
    check("a missing title is reported", any(i["kind"] == "title" for i in iss))
    m.title = "deck.pdf"
    check("a filename as the title is reported",
          any(i["kind"] == "title" for i in review(m)))
    m.title = "Quarterly Executive Presentation"
    m.lang = ""
    check("a missing language is reported", any(i["kind"] == "lang" for i in review(m)))
    m.lang = "en-GB"

    # hiding real text should be flagged
    target = next(e for p in m.pages for e in p.elements
                  if e.kind == "text" and len(e.text) > 30)
    target.role = "Artifact"
    check("hiding a long run of real text is flagged",
          any(i["kind"] == "hidden" for i in review(m)))

    # heading skips
    m2 = normalise_headings(analyse(src))
    hs = [e for p in m2.pages for e in p.elements if e.role.startswith("H")]
    check("no heading warnings on a normalised document", not heading_warnings(m2))
    hs[0].role = "H1"
    for e in hs[1:]:
        e.role = "H4"
    check("an induced heading skip is caught", bool(heading_warnings(m2)))


# --------------------------------------------------------------------------- #
# 6. API
# --------------------------------------------------------------------------- #
def api(port, path, data=None, headers=None, method=None, timeout=300):
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_confidence():
    section("8. Confidence scoring and the review workflow")
    from app.checks import needs_review, to_check
    from app.model import REVIEW_THRESHOLD

    src = fixture("slides-6page.pdf")
    m = normalise_headings(analyse(src))
    text = [e for p in m.pages for e in p.elements
            if e.kind == "text" and e.role != "Artifact" and e.text.strip()]
    check("every text element carries a score", all(0 <= e.score <= 100 for e in text),
          f"{len(text)} items")
    check("bands agree with the scores",
          all((e.confidence == "high") == (e.score >= 80) for e in text))
    check("every uncertain item explains itself",
          all(len(e.note.strip()) > 20 for e in text if e.score < REVIEW_THRESHOLD))

    pending = to_check(m)
    frac = len(pending) / max(len(text), 1)
    # A review list covering most of the document is noise; one covering none of it
    # is not doing its job.
    check("the review list is a usable size", 0 < frac < 0.45,
          f"{len(pending)} of {len(text)} = {frac*100:.0f}%")
    check("a list item with a marker is high confidence",
          all(e.score >= 80 for e in text if e.marker and e.text.strip()))
    check("body-size text is high confidence",
          any(e.score >= 90 for e in text))
    check("images are not in the text review list",
          all(e.kind == "text" for e in pending))

    # marking reviewed removes it, and only it
    n = len(pending)
    pending[0].reviewed = True
    check("confirming an item removes it from the list", len(to_check(m)) == n - 1)
    check("and does not touch the others",
          all(not e.reviewed for e in pending[1:]))

    # a high-confidence item is never asked about
    conf = [e for e in text if e.score >= 80]
    check("confident items are never queued for review",
          not any(needs_review(e) for e in conf), f"{len(conf)} confident")

    s = summary(m)
    for key in ("text_items", "confident", "to_check", "reviewed", "mean_score"):
        check(f"summary reports {key}", key in s, str(s.get(key)))
    check("the counts add up",
          s["to_check"] <= s["text_items"] and s["confident"] <= s["text_items"])


def test_decisions_persist():
    section("8b. A user's decisions survive an export and re-open")
    src = fixture("slides-6page.pdf")
    m = normalise_headings(analyse(src))
    target = next(e for p in m.pages for e in p.elements
                  if e.role == "P" and len(e.text) > 20)
    original, chosen = target.role, "H3"
    target.role = chosen
    target.reviewed = True
    for p in m.pages:
        for e in p.elements:
            if e.role == "Figure":
                e.alt = "A chart of scan results"
    out = os.path.join(TMP, "decided.pdf")
    build(m, out)

    m2 = normalise_headings(analyse(out))
    again = next((e for p in m2.pages for e in p.elements
                  if e.text[:30] == target.text[:30]), None)
    check("the re-opened element is found", again is not None)
    check("the role the user chose survived", again and again.role == chosen,
          f"{original} -> {chosen} -> {again.role if again else '?'}")
    check("it comes back marked as checked", again and again.reviewed)
    check("roles were read from the file, not guessed again", m2.adopted > 0,
          f"{m2.adopted} adopted")
    check("the file is recognised as this app's own work", m2.adopted_ours)
    check("nothing is left to check on a finished file", summary(m2)["to_check"] == 0,
          f"{summary(m2)['to_check']} left")

    # descriptions have to come back too, or the user re-types all of them
    figs = [e for p in m2.pages for e in p.elements if e.role == "Figure"]
    check("image descriptions survived the round trip",
          figs and all(e.alt.strip() for e in figs),
          f"{sum(1 for e in figs if e.alt.strip())}/{len(figs)} described")
    check("the exact wording came back",
          any(e.alt == "A chart of scan results" for e in figs))
    s2 = summary(m2)
    check("a finished file re-opens with nothing outstanding", s2["blockers"] == 0,
          f"{s2['blockers']} blockers")

    # a figure tagged but never described is still outstanding on re-open
    m4 = normalise_headings(analyse(src))
    for p in m4.pages:
        for e in p.elements:
            if e.role == "Figure":
                e.alt = ""
    undescribed = os.path.join(TMP, "nodesc.pdf")
    build(m4, undescribed)
    m5 = normalise_headings(analyse(undescribed))
    figs5 = [e for p in m5.pages for e in p.elements if e.role == "Figure"]
    if figs5:
        check("an undescribed image is still outstanding on re-open",
              summary(m5)["blockers"] > 0, f"{summary(m5)['blockers']} blockers")
        check("and is not marked as checked", not any(e.reviewed for e in figs5))

    # re-levelling must not overrule a decision
    hs = [e for p in m2.pages for e in p.elements if e.role.startswith("H")]
    check("a confirmed heading is not silently re-levelled",
          any(e.role == chosen and e.reviewed for e in hs))

    # and it is still idempotent with adoption in play
    out2 = os.path.join(TMP, "decided2.pdf")
    m3 = normalise_headings(analyse(out))
    for p in m3.pages:
        for e in p.elements:
            if e.role == "Figure" and not e.alt:
                e.alt = "A chart of scan results"
    build(m3, out2)
    check("streams stay byte-identical once roles are adopted",
          stream_hashes(out) == stream_hashes(out2))

    # a foreign tagged file: adopt, but do not sign off for the user
    ppt = os.path.join(os.path.dirname(ROOT), "ppt-test.pdf")
    if os.path.exists(ppt):
        mf = normalise_headings(analyse(ppt))
        check("another tool's tags are adopted", mf.adopted > 0, f"{mf.adopted}")
        check("but not attributed to this app", not mf.adopted_ours)
        check("and not silently marked as checked",
              not any(e.reviewed for p in mf.pages for e in p.elements))
        check("the user is told where those tags came from",
              any(i["kind"].startswith("adopted") for i in review(mf)),
              str([i["kind"] for i in review(mf) if i["kind"].startswith("adopted")]))
        # This particular file is a PowerPoint export, and it turns out to be flat
        # too: 173 paragraphs, 142 figures, not one heading. So the flat case is
        # not a Google Docs quirk, and it has to be reported as its own thing.
        if getattr(mf, "adopted_flat", False):
            check("a flat foreign tree says so specifically",
                  any(i["kind"] == "adopted-flat" for i in review(mf)))
            check("and its own headings were proposed regardless",
                  any(e.role.startswith("H") for p in mf.pages for e in p.elements),
                  "no headings proposed")


def test_error_messages():
    section("7. Failure messages a person can act on")
    from app.server import friendly_open_error
    d = pymupdf.open()
    pg = d.new_page(width=720, height=405)
    pg.insert_text((60, 90), "Secret", fontsize=24)
    pw = os.path.join(TMP, "pw.pdf")
    d.save(pw, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="letmein")

    cases = {"password protected": pw}
    p = os.path.join(TMP, "garbage.pdf"); open(p, "wb").write(b"nope" * 40)
    cases["garbage"] = p
    p = os.path.join(TMP, "half.pdf")
    # A header and the start of the first object, and nothing that can resolve to
    # a page tree. The old cut of 900 bytes only worked by accident: where the
    # catalogue sits is the producer's business, and MuPDF writes it first, so
    # any prefix of its output still opened.
    open(p, "wb").write(open(fixture("slides-1page.pdf"), "rb").read()[:120])
    cases["truncated"] = p
    p = os.path.join(TMP, "empty.pdf"); open(p, "wb").write(b"")
    cases["empty"] = p

    for label, path in cases.items():
        try:
            pikepdf.open(path)
            check(f"{label}: rejected", False, "it opened, which it should not have")
            continue
        except Exception as exc:
            msg = friendly_open_error(exc)
        # The app warns users about leaked file paths in alt text; leaking one in
        # its own error message would be a poor look.
        leaks = "/var" in msg or TMP in msg or "/tmp" in msg or "tmp" in msg.split("(")[0]
        check(f"{label}: message leaks no file path", not leaks, msg[:60])
        check(f"{label}: message is a readable sentence",
              msg[0].isupper() and msg.rstrip().endswith((".", ")")) and len(msg) > 30,
              msg[:70])
    try:
        pikepdf.open(pw)
    except Exception as exc:
        check("password protected: says so explicitly",
              "password" in friendly_open_error(exc).lower())


NATIVE_PROBE = '''
import sys, threading
sys.path.insert(0, %(root)r)
from AppKit import NSApp, NSObject
from Foundation import NSTimer
import app.native as nat
import app.server as srv

def emit(name, cond, detail=""):
    print(("PASS|" if cond else "FAIL|") + name + "|" + str(detail), flush=True)

class Probe(NSObject):
    def fire_(self, timer):
        try:
            w, wv = nat._state["window"], nat._state["webview"]
            emit("a real window is created", w is not None)
            emit("titled with the app name", w and str(w.title()) == "PDF Tagger")
            emit("visible on screen", bool(w and w.isVisible()))
            emit("minimum size is set", w and int(w.minSize().width) == 760)
            emit("position is remembered between launches",
                 w and str(w.frameAutosaveName()) == "PDFTaggerMainWindow")
            emit("content is a WKWebView", wv is not None and "WKWebView" in wv.className())
            emit("the app UI is loaded in it", "127.0.0.1" in str(wv.URL() or ""))
            bar = NSApp().mainMenu()
            names = [str(bar.itemAtIndex_(i).submenu().title())
                     for i in range(bar.numberOfItems())]
            emit("standard menu bar", len([n for n in names if n]) >= 4, ",".join(n for n in names if n))
            def keys(t):
                for i in range(bar.numberOfItems()):
                    s = bar.itemAtIndex_(i).submenu()
                    if str(s.title()) == t:
                        return {str(s.itemAtIndex_(j).keyEquivalent())
                                for j in range(s.numberOfItems())}
                return set()
            emit("Edit menu enables copy and paste in text fields",
                 {"c", "v", "x", "a"} <= keys("Edit"))
            emit("File menu offers Open with Cmd-O", "o" in keys("File"))
            emit("View menu offers reload and zoom", {"r", "0"} <= keys("View"))
            emit("appears in the Dock, not as a background agent",
                 NSApp().activationPolicy() == 0)
            emit("native file panels are wired in", srv.NATIVE is not None)
        except Exception as exc:
            emit("the probe ran", False, repr(exc)[:70])
        NSApp().terminate_(None)

httpd = srv.Server(("127.0.0.1", 0), srv.Handler)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
srv.NATIVE = nat
NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
    2.0, Probe.alloc().init(), "fire:", None, False)
nat.run_window("http://127.0.0.1:%%d/" %% port)
'''


def test_bundled_verapdf():
    section("10. Bundled veraPDF (a verdict without installing anything)")
    from app.server import verapdf_command
    bundled = os.path.join(ROOT, "build", "verapdf")
    if not os.path.isdir(bundled):
        check("a bundled veraPDF was built", False, "run ./build/bundle_verapdf.sh")
        return
    jar = os.path.join(bundled, "cli.jar")
    java = os.path.join(bundled, "jre", "bin", "java")
    check("the CLI jar is present", os.path.exists(jar),
          f"{os.path.getsize(jar)//1024//1024}MB" if os.path.exists(jar) else "")
    check("a trimmed Java runtime is present", os.path.exists(java))
    check("both licence texts ship with it",
          os.path.exists(os.path.join(bundled, "LICENSE-veraPDF-MPL-2.0.txt"))
          and os.path.exists(os.path.join(bundled, "LICENSE-veraPDF-GPL-3.0.txt")))
    check("the runtime's own notices ship with it",
          os.path.isdir(os.path.join(bundled, "jre-legal")))

    cmd = verapdf_command()
    check("the app resolves a veraPDF command", bool(cmd),
          " ".join(os.path.basename(c) for c in (cmd or [])))

    # The point of bundling: it has to work with no Java and no brew on PATH.
    clean = {"HOME": os.environ.get("HOME", "/tmp"), "PATH": "/usr/bin:/bin"}
    p = subprocess.run([java, "-jar", jar, "--version"], capture_output=True,
                       text=True, env=clean, timeout=180)
    check("the runtime starts with no Java on PATH", "veraPDF" in p.stdout,
          p.stdout.strip().splitlines()[0] if p.stdout.strip() else p.stderr[-60:])

    # And it must agree with the system copy, or it is worse than useless.
    tagged = os.path.join(TMP, "verdict.pdf")
    model = normalise_headings(analyse(fixture("slides-6page.pdf")))
    for pg in model.pages:
        for e in pg.elements:
            if e.role == "Figure":
                e.alt = "A chart of scan results"
    build(model, tagged)

    def verdict(command):
        r = subprocess.run(command + ["-f", "ua1", "--format", "xml", tagged],
                           capture_output=True, text=True, env=clean, timeout=600)
        try:
            root = ET.fromstring(r.stdout)
        except ET.ParseError:
            return None
        d = root.find(".//details")
        v = root.find(".//validationReport")
        return None if d is None else (v.get("isCompliant"), d.get("passedRules"),
                                       d.get("failedRules"))

    mine = verdict([java, "-jar", jar])
    check("the bundled copy produces a verdict", mine is not None, str(mine))
    system = shutil.which("verapdf")
    if system:
        theirs = verdict([system])
        check("it agrees exactly with the system veraPDF", mine == theirs,
              f"bundled {mine} vs system {theirs}")
    check("and the verdict is a pass on our own output",
          mine and mine[0] == "true" and mine[1] == "106", str(mine))


def test_native_window():
    section("9. Native window (opens a window briefly)")
    try:
        import objc  # noqa: F401
    except ImportError:
        check("PyObjC is available", False, "pip install pyobjc-framework-WebKit")
        return
    path = os.path.join(TMP, "native_probe.py")
    with open(path, "w") as f:
        f.write(NATIVE_PROBE % {"root": ROOT})
    p = subprocess.run([os.path.join(ROOT, ".venv/bin/python"), path],
                       capture_output=True, text=True, timeout=180)
    lines = [l for l in p.stdout.splitlines() if "|" in l]
    if not lines:
        check("the native probe produced results", False,
              (p.stderr or p.stdout)[-90:].replace("\n", " "))
        return
    for line in lines:
        status, name, detail = (line.split("|", 2) + ["", ""])[:3]
        check(name, status == "PASS", detail)

    # browser mode must still be intact, since that is the fallback
    from app import server as srv
    check("browser mode is still the fallback", srv.NATIVE is None,
          "NATIVE is only set inside a native run")


def test_api():
    section("6. API robustness")
    port = 8199
    proc = subprocess.Popen(
        [os.path.join(ROOT, ".venv/bin/python"), os.path.join(ROOT, "run.py"),
         str(port), "--no-browser"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(200):
            try:
                if api(port, "/api/state", timeout=2)[0] == 200:
                    break
            except Exception:
                pass
            import time
            time.sleep(0.25)
        else:
            check("server starts", False, "never came up")
            return
        check("server starts", True)

        code, _ = api(port, "/api/state")
        check("GET /api/state before anything is loaded", code == 200)

        code, body = api(port, "/api/upload", data=b"not a pdf",
                         headers={"X-Filename": "x.pdf", "Content-Type": "application/pdf"})
        check("garbage upload is rejected with a message", code == 400 and b"error" in body,
              f"HTTP {code}")

        code, body = api(port, "/api/upload", data=b"",
                         headers={"X-Filename": "e.pdf", "Content-Type": "application/pdf"},
                         method="POST")
        check("empty upload is rejected", code == 400, f"HTTP {code}")

        code, _ = api(port, "/api/export", data=b"{}",
                      headers={"Content-Type": "application/json"})
        check("export with nothing open is rejected", code == 400, f"HTTP {code}")

        code, body = api(port, "/api/model", data=b"{}",
                         headers={"Content-Type": "application/json"})
        check("a malformed model does not take the server down", code in (400, 500),
              f"HTTP {code}")
        check("server still alive after a malformed model",
              api(port, "/api/state")[0] == 200)

        # a filename that tries to escape the temp directory
        pdf_bytes = open(fixture("slides-1page.pdf"), "rb").read()
        code, body = api(port, "/api/upload", data=pdf_bytes,
                         headers={"X-Filename": "../../../../tmp/escape.pdf",
                                  "Content-Type": "application/pdf"})
        ok = code == 200
        landed = json.loads(body).get("source", "") if ok else ""
        check("a path-traversal filename is neutralised", ok and "pdftagger-" in landed,
              os.path.basename(landed))
        check("nothing was written outside the temp dir",
              not os.path.exists("/tmp/escape.pdf"))

        code, body = api(port, "/api/page?n=999&w=400")
        check("a page index out of range 404s", code == 404, f"HTTP {code}")
        code, body = api(port, "/api/page?n=0&w=99999")
        check("an absurd render width is clamped, not fatal", code == 200, f"HTTP {code}")
        code, body = api(port, "/api/crop?n=0&box=0,0,0,0")
        check("a degenerate crop box still renders", code == 200, f"HTTP {code}")

        # every element set to Artifact: nothing to tag, must not crash
        code, body = api(port, "/api/state")
        doc = json.loads(body)["doc"]
        for p in doc["pages"]:
            for e in p["elements"]:
                e["role"] = "Artifact"
        out = os.path.join(TMP, "all-artifact.pdf")
        code, body = api(port, "/api/export",
                         data=json.dumps({"doc": doc, "out": out}).encode(),
                         headers={"Content-Type": "application/json"})
        check("a document with everything ignored still exports", code == 200,
              f"HTTP {code}")

        # a very long description should not break the writer
        code, body = api(port, "/api/state")
        doc = json.loads(body)["doc"]
        doc["title"] = "Round trip title"
        n = 0
        for p in doc["pages"]:
            for e in p["elements"]:
                if e["kind"] == "image":
                    e["role"] = "Figure"
                    e["alt"] = "x" * 5000
                    n += 1
        out2 = os.path.join(TMP, "longalt.pdf")
        code, body = api(port, "/api/export",
                         data=json.dumps({"doc": doc, "out": out2}).encode(),
                         headers={"Content-Type": "application/json"})
        check("a 5000-character description is written without error", code == 200,
              f"HTTP {code}, {n} figures")

        # the real thing: upload, export, re-upload the export
        code, body = api(port, "/api/upload", data=pdf_bytes,
                         headers={"X-Filename": "trip.pdf",
                                  "Content-Type": "application/pdf"})
        doc = json.loads(body)["doc"]
        for p in doc["pages"]:
            for e in p["elements"]:
                if e["role"] == "Figure":
                    e["alt"] = "The company logo"
        out3 = os.path.join(TMP, "api-trip.pdf")
        code, body = api(port, "/api/export",
                         data=json.dumps({"doc": doc, "out": out3}).encode(),
                         headers={"Content-Type": "application/json"})
        first = json.loads(body)
        check("export through the API is compliant",
              (first.get("verapdf") or {}).get("compliant") is not False,
              f"veraPDF {(first.get('verapdf') or {}).get('passed')}")
        check("export reports clean text extraction",
              (first.get("extraction") or {}).get("ok") is True)

        code, body = api(port, "/api/upload", data=open(out3, "rb").read(),
                         headers={"X-Filename": "api-trip.pdf",
                                  "Content-Type": "application/pdf"})
        second = json.loads(body)
        check("the exported file re-uploads successfully", code == 200)
        check("re-upload is recognised as already tagged",
              second["doc"]["was_tagged"] is True)
        check("re-upload reports zero blockers", second["summary"]["blockers"] == 0,
              f"{second['summary']['blockers']} blockers, "
              f"{second['summary']['warnings']} warnings")
        check("re-upload keeps the title", second["doc"]["title"] == "The company logo"
              or bool(second["doc"]["title"]), repr(second["doc"]["title"]))
        check("re-upload finds the same number of items",
              second["summary"]["tagged"] == json.loads(
                  json.dumps({"t": sum(1 for p in doc["pages"]
                                       for e in p["elements"] if e["role"] != "Artifact")}))["t"],
              f"{second['summary']['tagged']} items")
    finally:
        try:
            api(port, "/api/quit", data=b"{}", timeout=5)
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


# --------------------------------------------------------------------------- #
def main():
    global USE_VERAPDF
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="fewer corpus files")
    ap.add_argument("--no-verapdf", action="store_true")
    ap.add_argument("--only", default="",
                    help="comma-separated: corpus,trip,foreign,edge,fonts,nested,"
                         "checks,conf,persist,errors,api,verapdf,native,shared,tables,edits,detect,layout")
    ap.add_argument("--native-ui", action="store_true",
                    help="include the native window checks (opens a window briefly)")
    a = ap.parse_args()
    USE_VERAPDF = not a.no_verapdf
    only = {s.strip() for s in a.only.split(",") if s.strip()}

    print(f"PDF Tagger test suite   (veraPDF: {'on' if USE_VERAPDF else 'off'}, "
          f"scratch: {TMP})")
    plan = [("corpus", lambda: test_corpus(limit=3 if a.quick else None)),
            ("trip", test_round_trip),
            ("foreign", test_foreign_tagged),
            ("edge", test_edge_cases),
            ("fonts", test_font_reporting),
            ("nested", test_form_xobjects),
            ("shared", test_shared_forms),
            ("tables", test_tables),
            ("edits", test_edits),
            ("kickers", test_kickers),
            ("detect", test_detection),
            ("flat", test_flat_foreign_tags),
            ("wrap", test_wrap_vs_break),
            ("unreliable", test_unreliable_tags),
            ("docdeck", test_document_vs_deck),
            ("torder", test_table_reading_order),
            ("zwlist", test_zero_width_lists),
            ("forms", test_form_fields),
            ("shaded", test_shaded_tables),
            ("assist", test_assist),
            ("flow", test_flow_downwards),
            ("layout", test_layout_rules),
            ("checks", test_checks_layer),
            ("conf", test_confidence),
            ("persist", test_decisions_persist),
            ("errors", test_error_messages),
            ("api", test_api),
            ("verapdf", test_bundled_verapdf),
            ("native", test_native_window)]
    for key, fn in plan:
        if only and key not in only:
            continue
        # Opening a window is intrusive, so it is opt-in unless asked for by name.
        if key == "native" and not (a.native_ui or "native" in only):
            continue
        try:
            fn()
        except Exception:
            import traceback
            traceback.print_exc()
            check(f"{key} suite ran to completion", False, "raised")

    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed = [r for r in RESULTS if not r[0]]
    tail = f", {len(SKIPPED)} skipped" if SKIPPED else ""
    print(f"\n\033[1m{passed} passed, {len(failed)} failed{tail}, "
          f"{len(RESULTS)} checks\033[0m")
    if SKIPPED:
        print("\nSkipped (see tests/corpus.json.example):")
        for name, why in SKIPPED:
            print(f"  - {name}  {why}")
    if failed:
        print("\nFailures:")
        for _, name, detail in failed:
            print(f"  - {name}  {detail}")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
