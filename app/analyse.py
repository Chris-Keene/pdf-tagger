"""Read a PDF and propose a structure for it.

Two views of the same page are combined:

* `pymupdf` gives the *semantic* view — text grouped into blocks, lines and spans
  with font sizes and bounding boxes.  This is what decides "this is a heading".
* a content-stream walk gives the *mechanical* view — the operator ranges that
  actually paint each mark.  This is what the tagger needs in order to wrap the
  right operators in the right marked-content sequence.

The two are joined on position: a text object whose origin sits inside a line's
box belongs to that line's element.
"""
import re
import pikepdf
import pymupdf

from .model import Cell, DocModel, PageModel, Element, Table

ZERO_WIDTH = "\u200b\ufeff\u00ad"


def _visible(text):
    return (text or "").translate({ord(c): None for c in ZERO_WIDTH}).strip()


BULLETS = "\u2022\u25cf\u25cb\u25e6\u25aa\u25ab\u25fc\u25fb\u2013\u2014-\u2023\u2043\u2219\u00b7*\u25b8\u25b9"
# The trailing space is optional, because a numbered list exported from Slides
# puts the number in its own text object: the line is just "1." with the words
# beside it. Requiring text after the marker meant those were never recognised as
# a list at all, and the number and its item came out as two paragraphs.
_ORDERED = re.compile(r"^\s*(\(?\d{1,2}[.)\]]|[a-zA-Z][.)]|[ivxIVX]{1,4}[.)])(?:\s+|$)")
_PAINT_OPS = {"f", "F", "f*", "B", "B*", "b", "b*", "S", "s", "sh", "EI"}


def _mul(a, b):
    a0, a1, a2, a3, a4, a5 = a
    b0, b1, b2, b3, b4, b5 = b
    return (a0 * b0 + a1 * b2, a0 * b1 + a1 * b3,
            a2 * b0 + a3 * b2, a2 * b1 + a3 * b3,
            a4 * b0 + a5 * b2 + b4, a4 * b1 + a5 * b3 + b5)


_SHOW_TEXT = {"Tj", "TJ", "'", '"'}


def form_contents(xobj, depth=0, seen=None):
    """What a form XObject carries: (paints_text, has_marked_content).

    Both matter, for different reasons.  Text inside a form lives in the form's
    own stream, so a page-level pass cannot reach it.  Marked content inside a
    form means the form is already tagged, and wrapping its `Do` as an artifact
    would put tagged content inside an artifact — which fails PDF/UA 7.1 t2 and
    orphans it.
    """
    if depth > 4:
        return False, False
    seen = seen if seen is not None else set()
    try:
        key = xobj.objgen
        if key in seen:
            return False, False
        seen.add(key)
    except Exception:
        pass
    try:
        ins = pikepdf.parse_content_stream(xobj)
    except Exception:
        return False, False
    text = marked = False
    for item in ins:
        op = str(item.operator)
        if op in _SHOW_TEXT:
            text = True
        elif op in ("BDC", "BMC"):
            marked = True
    nested = xobj.get("/Resources", {}).get("/XObject", {}) or {}
    for _n, v in nested.items():
        if str(v.get("/Subtype", "")) == "/Form":
            t2, m2 = form_contents(v, depth + 1, seen)
            text = text or t2
            marked = marked or m2
    return text, marked


def walk_streams(page):
    """Walk the page *and* the form XObjects it draws.

    Text inside a form lives in the form's own content stream, so a page-level
    walk cannot see it and the app used to report it as unreachable. Recursing
    gives every mark an operator range plus the identity of the stream that range
    indexes into, which is what the tagger needs in order to rewrite the right
    stream and to reference the right marked content afterwards.

    Positions are composed all the way down, so everything comes back in page
    space and the join with the layout view is unchanged.

    Returns (streams, units):
      streams: {key: {"owner": <stream object>, "ins": [instructions]}}
               key is "" for the page itself, else "objnum,gen" for a form.
      units:   as walk_content, plus "stream" naming the key it belongs to.
    """
    streams, units = {}, []
    seen_forms = set()

    def visit(owner, key, base_ctm, depth):
        if depth > 6:
            return
        try:
            ins = pikepdf.parse_content_stream(owner)
        except Exception:
            return
        streams[key] = {"owner": owner, "ins": ins}
        xobjs = (owner.get("/Resources") or {}).get("/XObject", {}) or {}
        ctm = base_ctm
        stack = []
        bt = tm = fsize = font = None
        for i, item in enumerate(ins):
            op = str(item.operator)
            rands = item.operands
            if op == "q":
                stack.append(ctm)
            elif op == "Q":
                ctm = stack.pop() if stack else ctm
            elif op == "cm":
                # A malformed cm, with the wrong number of operands or none
                # at all, is not worth taking the whole document down for.
                # One real deck ships a bare `cm`; skipping it leaves the
                # transform as it was, which is what a viewer does too.
                vals = tuple(float(x) for x in rands)
                if len(vals) == 6:
                    ctm = _mul(vals, ctm)
            elif op == "BT":
                bt, tm = i, (1, 0, 0, 1, 0, 0)
            elif op == "Tf":
                font, fsize = str(rands[0]), float(rands[1])
            elif op == "Tm":
                tm = tuple(float(x) for x in rands)
            elif op == "ET" and bt is not None:
                m = _mul(tm, ctm)
                scale = (m[2] ** 2 + m[3] ** 2) ** 0.5 or 1.0
                units.append({"kind": "text", "ops": (key, bt, i), "stream": key,
                              "x": m[4], "y": m[5], "size": (fsize or 0) * scale,
                              "font": font})
                bt = None
            elif op == "Do":
                nm = str(rands[0])
                xo = xobjs.get(nm)
                sub = str(xo.get("/Subtype", "")) if xo is not None else ""
                pts = [(cx * ctm[0] + cy * ctm[2] + ctm[4],
                        cx * ctm[1] + cy * ctm[3] + ctm[5])
                       for cx, cy in ((0, 0), (1, 0), (1, 1), (0, 1))]
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                if sub == "/Form":
                    try:
                        n, g = xo.objgen
                        fkey = f"{n},{g}"
                    except Exception:
                        fkey = None
                    units.append({"kind": "form", "ops": (key, i, i), "stream": key,
                                  "name": nm, "form": fkey,
                                  "x0": min(xs), "y0": min(ys),
                                  "x1": max(xs), "y1": max(ys)})
                    if fkey is not None and fkey not in seen_forms:
                        seen_forms.add(fkey)
                        mat = xo.get("/Matrix")
                        inner = _mul(tuple(float(v) for v in mat), ctm) \
                            if mat is not None else ctm
                        visit(xo, fkey, inner, depth + 1)
                else:
                    units.append({"kind": "image", "ops": (key, i, i), "stream": key,
                                  "name": nm, "x0": min(xs), "y0": min(ys),
                                  "x1": max(xs), "y1": max(ys)})
            elif op in _PAINT_OPS:
                units.append({"kind": "paint", "ops": (key, i, i), "stream": key,
                              "x": ctm[4], "y": ctm[5]})

    visit(page, "", (1, 0, 0, 1, 0, 0), 0)
    return streams, units


def walk_content(page):
    """Classify every instruction in the page's content stream.

    Returns (instructions, units) where each unit is a dict describing something
    that puts marks on the page, with the instruction index range that does it.
    """
    ins = pikepdf.parse_content_stream(page)
    xobjs = page.get("/Resources", {}).get("/XObject", {})
    ctm = (1, 0, 0, 1, 0, 0)
    stack = []
    units = []
    bt = None
    tm = None
    fsize = None
    font = None
    for i, item in enumerate(ins):
        op = str(item.operator)
        rands = item.operands
        if op == "q":
            stack.append(ctm)
        elif op == "Q":
            ctm = stack.pop() if stack else ctm
        elif op == "cm":
            ctm = _mul(tuple(float(x) for x in rands), ctm)
        elif op == "BT":
            bt, tm = i, (1, 0, 0, 1, 0, 0)
        elif op == "Tf":
            font, fsize = str(rands[0]), float(rands[1])
        elif op == "Tm":
            tm = tuple(float(x) for x in rands)
        elif op == "ET" and bt is not None:
            m = _mul(tm, ctm)
            scale = (m[2] ** 2 + m[3] ** 2) ** 0.5 or 1.0
            units.append({"kind": "text", "ops": (bt, i), "x": m[4], "y": m[5],
                          "size": (fsize or 0) * scale, "font": font})
            bt = None
        elif op == "Do":
            nm = str(rands[0])
            sub = str(xobjs.get(nm, {}).get("/Subtype", "")) if nm in xobjs else ""
            pts = [(cx * ctm[0] + cy * ctm[2] + ctm[4],
                    cx * ctm[1] + cy * ctm[3] + ctm[5])
                   for cx, cy in ((0, 0), (1, 0), (1, 1), (0, 1))]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            is_form = sub == "/Form"
            has_text = has_marked = False
            if is_form and nm in xobjs:
                has_text, has_marked = form_contents(xobjs[nm])
            units.append({"kind": "form" if is_form else "image",
                          "ops": (i, i), "name": nm,
                          "has_text": has_text, "has_marked": has_marked,
                          "x0": min(xs), "y0": min(ys),
                          "x1": max(xs), "y1": max(ys)})
        elif op in _PAINT_OPS:
            units.append({"kind": "paint", "ops": (i, i), "x": ctm[4], "y": ctm[5]})
    return ins, units


def _spans_of(page_dict):
    """Flatten pymupdf's block/line/span tree into lines with useful attributes."""
    lines = []
    for blk in page_dict["blocks"]:
        if blk.get("type") != 0:
            continue
        for ln in blk["lines"]:
            spans = [s for s in ln["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = "".join(s["text"] for s in ln["spans"])
            # The size of a line is what most of it is set at, not its largest
            # span. A bold lead-in word set two points up — "**Tooling** helps
            # devs ship new features accessibly" — otherwise made the whole line
            # read as 16pt bold, so it failed to join its own 14pt second line and
            # the sentence split in two, the first half becoming a heading.
            big = max(spans, key=lambda s: (len(s["text"].strip()), s["size"]))
            lines.append({
                "block": blk["number"],
                "text": text,
                "bbox": ln["bbox"],
                "size": round(big["size"], 1),
                "font": big["font"],
                "bold": "bold" in big["font"].lower() or "black" in big["font"].lower()
                        or "semibold" in big["font"].lower(),
            })
    return lines


def _strip_marker(text):
    """Return (marker, rest, ordered) for a list-looking line."""
    t = text.lstrip()
    if t[:1] in BULLETS:
        return t[0], t[1:].strip(), False
    m = _ORDERED.match(t)
    if m:
        return m.group(1), t[m.end():].strip(), True
    return "", text, False


# A shape covering this much of the page is the background, not a container.
_SHAPE_MAX_AREA = 0.55
# Text sharing a background reads as one phrase only while it stays short. A card
# holding a heading and a paragraph is one container but two things.
_SHAPE_MERGE_WORDS = 8


def _shapes(page, W, H):
    """Filled shapes on the page that could be holding text.

    Slides put callouts in a coloured circle or a rounded card, and the shape is
    the only thing saying that the words inside belong together: "4X" and "better"
    are two text objects a long way apart in the content stream, and nothing but
    the circle around them says they are one phrase. The page's own background
    fill is excluded, or everything would be "inside a shape".
    """
    out = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return out
    area = max(W * H, 1)
    for dr in drawings:
        if not dr.get("fill"):
            continue
        r = dr.get("rect")
        if r is None or r.width <= 4 or r.height <= 4:
            continue
        if (r.width * r.height) / area > _SHAPE_MAX_AREA:
            continue
        out.append((r.x0, r.y0, r.x1, r.y1))
    return out


def _shape_of(bbox, shapes):
    """Index of the smallest shape containing a box's centre, or None."""
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    best, bi = None, None
    for i, (x0, y0, x1, y1) in enumerate(shapes):
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            a = (x1 - x0) * (y1 - y0)
            if best is None or a < best:
                best, bi = a, i
    return bi


def _in_different_shapes(a, b, shapes):
    """Whether two boxes sit in two different containers.

    Both have to be in a shape for this to separate them. "In a shape" versus "in
    no shape" is not a boundary worth splitting on, because it is mostly what
    happens when a wrapped sentence starts inside a rounded card and its last line
    hangs a few points below it: the sentence would be torn in half.
    """
    if not shapes:
        return False
    sa, sb = _shape_of(a, shapes), _shape_of(b, shapes)
    return sa is not None and sb is not None and sa != sb


# Merging on a shared background is only safe for a badge: something small and
# roughly as wide as it is tall, like the roundel holding "4X better". A wide card
# is a container for a label *and* its description, which are two things, and a
# large panel can sit behind half a slide. Both were welding unrelated text
# together, and inconsistently, since it depended on which decoration happened to
# overlap what.
_SHAPE_MERGE_MAX_AREA = 0.035
_SHAPE_MERGE_MAX_ASPECT = 2.2


def _merge_in_shapes(elements, shapes, page_area):
    """Join short text that shares a background shape into one item."""
    if not shapes or not page_area:
        return elements
    groups = {}
    for e in elements:
        if e.kind != "text" or e.role == "Artifact" or not (e.text or "").strip():
            continue
        i = _shape_of(e.bbox, shapes)
        if i is None:
            continue
        x0, y0, x1, y1 = shapes[i]
        w, h = x1 - x0, y1 - y0
        if w * h > page_area * _SHAPE_MERGE_MAX_AREA:
            continue                 # a panel, not a badge
        if max(w, h) / max(1.0, min(w, h)) > _SHAPE_MERGE_MAX_ASPECT:
            continue                 # a card holding a label and its description
        groups.setdefault(i, []).append(e)
    drop = set()
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda e: (e.bbox[1], e.bbox[0]))
        # Only a run of lines that actually sit against each other. Three pairs of
        # figures stacked down one card are three things, not one: merging the
        # whole container gave "2021 Launch Year $110M Capital Raised 100+".
        runs, cur_run = [], [members[0]]
        for prev, e in zip(members, members[1:]):
            gap = e.bbox[1] - prev.bbox[3]
            if gap <= max(prev.size, e.size) * 1.2:
                cur_run.append(e)
            else:
                runs.append(cur_run)
                cur_run = [e]
        runs.append(cur_run)
        for run in runs:
            _merge_run(run, drop)
    return [e for e in elements if e.id not in drop]


def _merge_run(members, drop):
    if len(members) < 2:
        return
    words = sum(len((e.text or "").split()) for e in members)
    if words > _SHAPE_MERGE_WORDS:
        return                       # a card holding real content, not a callout
    keep = members[0]
    for e in members[1:]:
        keep.text = (keep.text + " " + (e.text or "")).strip()
        keep.bbox = (min(keep.bbox[0], e.bbox[0]), min(keep.bbox[1], e.bbox[1]),
                     max(keep.bbox[2], e.bbox[2]), max(keep.bbox[3], e.bbox[3]))
        keep.ops = list(keep.ops) + list(e.ops)
        keep.parts = list(keep.parts or []) + list(e.parts or [])
        drop.add(e.id)
    keep.text = re.sub(r"\s+", " ", keep.text).strip()
    keep.note = ("Sits inside the same shape as the text next to it, so it "
                 "reads as one phrase.")


def _columns(lines, page_width):
    """Cluster line left edges into columns so reading order follows the layout.

    Slide decks are routinely two- or three-column; without this, reading order
    zig-zags across the page, which no conformance checker catches but every
    screen reader user notices.
    """
    if not lines:
        return {}
    lefts = sorted({round(l["bbox"][0]) for l in lines})
    gap = max(page_width * 0.08, 24)
    groups = [[lefts[0]]]
    for x in lefts[1:]:
        if x - groups[-1][-1] > gap:
            groups.append([x])
        else:
            groups[-1].append(x)
    # A column must hold real content, not one stray label.
    edges = [min(g) for g in groups]
    return {"edges": edges, "gap": gap}


def _column_of(bbox, cols):
    """Which column a box sits in: the last edge at or left of its own left edge.

    Deliberately *containing* rather than nearest. Nearest-edge assignment breaks
    on anything that is not left-aligned in its column: a number centred in a wide
    column can sit closer to the next column's edge than to its own, so two
    columns collapse into one and their text is merged into a single item. On a
    real table that turned "8" and "19" from different columns into one item
    reading "8 19".
    """
    if not cols:
        return 0
    x = bbox[0]
    bi = 0
    for i, e in enumerate(cols["edges"]):
        if x >= e - 1:
            bi = i
        else:
            break
    return bi


def font_problems(pdf):
    """Fonts whose programs are not stored inside the PDF.

    Nothing tagging does can fix this: the glyphs a reader needs simply are not
    in the file, so it fails conformance and can render differently elsewhere.
    It has to be fixed where the document was produced, so it is worth saying
    up front rather than letting veraPDF phrase it at the end.
    """
    missing, seen, type3 = [], set(), set()

    def scan(res, depth=0):
        if res is None or depth > 2:
            return
        for _name, f in (res.get("/Font", {}) or {}).items():
            base = str(f.get("/BaseFont", "?")).lstrip("/").split("+")[-1]
            if base in seen:
                continue
            seen.add(base)
            subtype = str(f.get("/Subtype", ""))
            if subtype == "/Type3":
                # Type3 glyphs are drawing procedures held in /CharProcs, so there
                # is no font file to embed and 7.21.4.1 does not apply.
                type3.add(base if base != "?" else "an unnamed Type 3 font")
                continue
            fd = f.get("/FontDescriptor")
            kids = f.get("/DescendantFonts")
            if kids is not None and len(kids):
                fd = kids[0].get("/FontDescriptor")
            embedded = fd is not None and any(
                k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3"))
            if not embedded and base != "?":
                missing.append(base)
        for _name, xo in (res.get("/XObject", {}) or {}).items():
            if str(xo.get("/Subtype", "")) == "/Form":
                scan(xo.get("/Resources"), depth + 1)

    for page in pdf.pages:
        scan(page.get("/Resources"))
    return missing, sorted(type3)


_ADOPTABLE = {"H1", "H2", "H3", "H4", "H5", "H6", "P", "Span", "BlockQuote",
              "Caption", "Figure", "LI", "Lbl", "LBody", "L", "Link", "Table",
              "TD", "TH", "TR"}


def read_existing_roles(pdf):
    """Recover the roles already recorded in the file's structure tree.

    Without this, re-opening a file the user has already worked on throws their
    decisions away and asks them to make every one again — which makes the whole
    idea of confirming a proposal pointless. An existing tag is better evidence
    than any guess from font sizes, whoever produced it.

    Returns {(page_index, mcid): {"role": ..., "alt": ...}}.
    """
    if "/StructTreeRoot" not in pdf.Root:
        return {}
    page_index = {}
    for i, page in enumerate(pdf.pages):
        try:
            page_index[page.obj.objgen] = i
        except Exception:
            pass

    out = {}
    seen = set()

    def walk(node, inherited=None):
        try:
            oid = node.objgen
            if oid in seen:
                return
            seen.add(oid)
        except Exception:
            pass
        stype = str(node.get("/S", "")).lstrip("/")

        # A list item's text hangs off /LBody, so the useful label is the /LI
        # above it, and whether the list is numbered comes from the /L above that.
        role = stype
        if stype in ("LBody", "Lbl"):
            role = inherited or "LI"
        elif stype == "LI":
            role = inherited or "LI"

        pg = node.get("/Pg")
        pno = None
        if pg is not None:
            try:
                pno = page_index.get(pg.objgen)
            except Exception:
                pno = None

        # Carry the description across too. Recovering the role but not the /Alt
        # would still make the user re-describe every image they had already done.
        alt = node.get("/Alt")
        alt = str(alt) if alt is not None else ""

        kids = node.get("/K")
        if kids is not None and role in _ADOPTABLE and pno is not None:
            items = kids if isinstance(kids, pikepdf.Array) else [kids]
            for k in items:
                if isinstance(k, int):
                    out[(pno, int(k))] = {"role": role, "alt": alt}

        # what to pass down
        pass_down = inherited
        if stype == "L":
            numbering = str(node.get("/A", {}).get("/ListNumbering", "")) \
                if isinstance(node.get("/A"), pikepdf.Dictionary) else ""
            pass_down = "LI-ordered" if numbering in ("/Decimal", "/UpperRoman",
                                                      "/LowerRoman", "/UpperAlpha",
                                                      "/LowerAlpha") else "LI"
        elif stype == "LI":
            pass_down = inherited or "LI"

        if kids is None:
            return
        items = kids if isinstance(kids, pikepdf.Array) else [kids]
        for k in items:
            if isinstance(k, pikepdf.Dictionary) and str(k.get("/Type", "")) == "/StructElem":
                walk(k, pass_down)

    root_k = pdf.Root.StructTreeRoot.get("/K")
    if root_k is None:
        return {}
    items = root_k if isinstance(root_k, pikepdf.Array) else [root_k]
    for k in items:
        if isinstance(k, pikepdf.Dictionary):
            walk(k, None)
    return out


def mcids_of_streams(page):
    """Innermost open marked-content id per instruction, for every stream.

    Returns {stream key: {instruction index: mcid}} so adoption can recover roles
    from form XObjects too, not just the page.
    """
    out = {}
    streams, _units = walk_streams(page)
    for key, info in streams.items():
        table, stack = {}, []
        for i, item in enumerate(info["ins"]):
            op = str(item.operator)
            if op in ("BDC", "BMC"):
                mcid = None
                if op == "BDC" and len(item.operands) > 1:
                    try:
                        mcid = int(item.operands[1]["/MCID"])
                    except Exception:
                        mcid = None
                stack.append(mcid)
            elif op == "EMC":
                if stack:
                    stack.pop()
            inner = next((m for m in reversed(stack) if m is not None), None)
            if inner is not None:
                table[i] = inner
        out[key] = table
    return out


def mcids_of_page(page):
    """For each instruction index, the innermost marked-content id enclosing it.

    Looking only just before a `BT` is not enough: this app writes the `BDC`
    immediately before the text object, but other producers wrap a whole region
    in one sequence, so the id can sit many operators earlier. Tracking what is
    *open* at each point is the semantics the spec actually describes.
    """
    try:
        ins = pikepdf.parse_content_stream(page)
    except Exception:
        return {}
    out = {}
    stack = []
    for i, item in enumerate(ins):
        op = str(item.operator)
        if op in ("BDC", "BMC"):
            mcid = None
            if op == "BDC" and len(item.operands) > 1:
                try:
                    mcid = int(item.operands[1]["/MCID"])
                except Exception:
                    mcid = None
            stack.append(mcid)
        elif op == "EMC":
            if stack:
                stack.pop()
        inner = next((m for m in reversed(stack) if m is not None), None)
        if inner is not None:
            out[i] = inner
    return out


def tagged_by_this_app(pdf):
    """Did this app write the tags in this file?

    It matters for how much to trust them. Our own tags mean a human already
    worked through the document, so re-asking them to confirm every item is just
    rude. Another tool's tags are still better evidence than a font-size guess,
    but nobody here has vouched for them.
    """
    try:
        if "/Metadata" not in pdf.Root:
            return False
        return b"PDF Tagger" in bytes(pdf.Root.Metadata.read_bytes())
    except Exception:
        return False


# Roles a foreign tool has demonstrably got right when it bothers to emit them:
# table geometry, figures and artifacts are mechanical, and a producer that writes
# them is telling us something we would otherwise have to guess.
_STRUCTURAL = {"Table", "TR", "TH", "TD", "THead", "TBody", "TFoot",
               "Figure", "Artifact", "Caption", "L", "LI", "LBody", "Lbl"}

# When the tags are not merely absent but actively misaligned, only the purely
# mechanical roles survive. Table geometry and images are drawn from the page
# rather than inferred from it, so they stay right even when text roles have
# slipped against the content.
_MECHANICAL = {"Table", "TR", "TH", "TD", "THead", "TBody", "TFoot",
               "Figure", "Artifact", "Caption"}


def flat_foreign_tree(existing, text_items):
    """Whether another tool's tags amount to a judgement about text at all.

    A producer that emits paragraphs and nothing else has not decided that this
    document has no headings; it simply does not write headings. Google Docs is
    the common case, and its export of a document with a title and four section
    headings comes back as 112 paragraphs and zero headings. Adopting that buries
    our own proposals under someone else's silence, and the file looks tagged
    while being unusable for the one thing screen reader users do most.

    So: no headings *and* no lists, across a document with real body text, means
    the text roles carry no information and ours stand instead. Its tables and
    figures are still adopted, because those it does get right.
    """
    kinds = {v["role"] for v in existing.values()}
    if any(k.startswith("H") for k in kinds):
        return False
    if {"L", "LI", "LBody"} & kinds:
        return False
    return text_items >= 10


def unreliable_tags(pairs):
    """Whether a file's own tags line up with what is actually on the page.

    A tree can be full of headings and lists and still be nonsense. Google Docs
    exports one where the roles are misaligned against the content: the 28pt
    document title carries /P while an 18pt line carries /H1, real sentences carry
    the inline role /Span, and a third of the block roles land on zero-width
    characters rather than on any text. Adopting that is worse than adopting
    nothing, because it looks considered.

    Two signals separate it from a genuinely tagged file, measured across a real
    corpus:

      * heading levels running backwards against size — H1 set smaller on average
        than H2. No sound file in the corpus does this; the broken one does.
      * block roles landing on invisible content. Sound files sat at 0 to 4 per
        cent, the broken one at 30.

    Returns (unreliable, reason).
    """
    text = [(e, r) for e, r in pairs if e.kind == "text"]
    if len(text) < 20:
        return False, ""
    heads = [(e.size, r) for e, r in text if r.startswith("H")]
    if not heads:
        return False, ""                 # nothing claimed; that is the flat case

    h1 = [sz for sz, r in heads if r == "H1"]
    h2 = [sz for sz, r in heads if r == "H2"]
    if h1 and h2 and (sum(h1) / len(h1)) < (sum(h2) / len(h2)) - 0.5:
        return True, ("its heading levels run backwards: the text marked as a top "
                      "level heading is set smaller than the text marked below it")

    blocks = [(e, r) for e, r in text
              if r in ("LI", "P", "LBody", "Lbl") or r.startswith("H")]
    if blocks:
        empty = sum(1 for e, _ in blocks if not _visible(e.text))
        if empty / len(blocks) >= 0.25:
            return True, (f"{round(100 * empty / len(blocks))}% of its tags point at "
                          "invisible characters rather than at any text")
    return False, ""


def _adopt_existing(pdf, model):
    """Let roles already in the file override the guesses."""
    existing = read_existing_roles(pdf)
    if not existing:
        return 0
    ours = tagged_by_this_app(pdf)
    text_items = sum(1 for p in model.pages for e in p.elements
                     if e.kind == "text" and (e.text or "").strip())
    # Gather first, judge second, apply third. Whether these tags are worth having
    # can only be answered by looking at all of them against the page.
    pairs = []
    for pno, page in enumerate(pdf.pages):
        by_stream = mcids_of_streams(page)
        if not by_stream:
            continue
        pm = model.pages[pno]
        for e in pm.elements:
            if not e.ops:
                continue
            rec = None
            for skey, a, _b in e.ops:
                mcid = by_stream.get(skey, {}).get(a)
                if mcid is None:
                    continue
                rec = existing.get((pno, mcid))
                if rec:
                    break
            if rec:
                pairs.append((e, rec))

    flat = (not ours) and flat_foreign_tree(existing, text_items)
    wrong, why = (False, "")
    if not ours and not flat:
        wrong, why = unreliable_tags([(e, r["role"]) for e, r in pairs])
    model.adopted_flat = flat or wrong
    model.adopted_reason = "wrong" if wrong else ("flat" if flat else "")
    model.adopted_why = why
    skip_text = flat or wrong

    adopted = 0
    for e, rec in pairs:
        found = rec["role"]
        if found not in _ADOPTABLE:
            continue
        # A flat foreign tree gets to keep its structural tags and nothing
        # else: its idea of what is a paragraph would otherwise overrule every
        # heading we found.
        # Lists are part of the text structure, so a tree that got the text wrong
        # got the lists wrong in the same way: in one export every /LI sat on a
        # zero-width character rather than on the item it was meant to mark.
        trusted = _MECHANICAL if wrong else _STRUCTURAL
        if skip_text and found not in trusted:
            continue
        if found in ("Lbl", "LBody", "L"):
            found = "LI"
        if found != e.role:
            e.role = found
        e.auto_role = found
        if e.kind == "image" and rec.get("alt") and not e.alt:
            e.alt = rec["alt"]
        if ours:
            e.score, e.confidence, e.reviewed = 100, "high", True
            e.note = ("Already settled: this came from the tags in the file, "
                      "which this app wrote.")
            if e.kind == "image" and not e.alt.strip():
                # Tagged as a figure last time but never described, so it is
                # still outstanding however trustworthy the role is.
                e.reviewed = False
                e.note = ("Tagged as an image last time but never described.")
        else:
            # Trust it enough to use, and say so once at the document level
            # rather than flagging all several hundred of them individually.
            e.score, e.confidence, e.reviewed = 75, "medium", False
            e.note = ("Taken from tags another tool put in this file. Better "
                      "than a guess, but nobody has checked it.")
        adopted += 1
    return adopted


def analyse(path, lang="en-GB"):
    pdf = pikepdf.open(path)
    doc = pymupdf.open(path)
    title = ""
    try:
        if pdf.trailer.get("/Info") and "/Title" in pdf.docinfo:
            title = str(pdf.docinfo["/Title"]).strip()
    except Exception:
        pass

    fonts_missing, type3 = font_problems(pdf)
    model = DocModel(path=path, title=title, lang=lang,
                     was_tagged="/StructTreeRoot" in pdf.Root,
                     unembedded_fonts=fonts_missing, type3_fonts=type3)

    # Body text size must be judged across the whole document: a title slide
    # holds nothing but a title, so a per-page estimate makes the title "body"
    # and it never gets proposed as a heading.
    all_lines = [_spans_of(doc[i].get_text("dict")) for i in range(len(doc))]
    body_size = _body_size([l for ls in all_lines for l in ls])

    # Landscape pages are slides; portrait pages are a document that flows across
    # them. The distinction decides whether each page gets its own top-level
    # heading, and it is a property of the file rather than a guess.
    _mb0 = [float(v) for v in pdf.pages[0].MediaBox]
    slides = (_mb0[2] - _mb0[0]) > (_mb0[3] - _mb0[1])

    for pno, page in enumerate(pdf.pages):
        mb = [float(v) for v in page.MediaBox]
        W, H = mb[2] - mb[0], mb[3] - mb[1]
        pm = PageModel(number=pno, width=W, height=H)
        _streams, units = walk_streams(page)
        lines = all_lines[pno]
        body_here = _page_body_size(lines, body_size)
        shapes = _shapes(doc[pno], W, H)
        cols = _columns(lines, W)

        # --- attach content-stream text objects to layout lines -------------
        text_units = [u for u in units if u["kind"] == "text"]
        for ln in lines:
            x0, y0, x1, y1 = ln["bbox"]
            ln["ops"] = []
            for u in text_units:
                if u.get("used"):
                    continue
                # pymupdf y grows downward; convert the origin to that space.
                uy = H - u["y"]
                if x0 - 2 <= u["x"] <= x1 + 2 and y0 - 2 <= uy <= y1 + 4:
                    ln["ops"].append(u["ops"])
                    u["used"] = True

        elements = _group(lines, body_here, pno, cols, H, shapes)
        elements = _merge_in_shapes(elements, shapes, W * H)

        # --- images ---------------------------------------------------------
        # Slides draws each image twice at the same spot, so the second draw is
        # redundant paint: describing it again would announce the same picture
        # twice.  Keep the first, artifact the rest.
        seen_at = {}
        for u in units:
            if u["kind"] != "image":
                continue
            spot = (round(u["x0"] / 2), round(u["y0"] / 2),
                    round(u["x1"] / 2), round(u["y1"] / 2))
            dup = spot in seen_at
            seen_at[spot] = True
            elements.append(Element(
                id=f"p{pno}i{len(elements)}", page=pno, kind="image",
                role="Artifact" if dup else "Figure",
                auto_role="Artifact" if dup else "Figure",
                bbox=(u["x0"], H - u["y1"], u["x1"], H - u["y0"]),
                ops=[u["ops"]], text="(image)",
                note="Painted a second time in the same place, so it is ignored "
                     "rather than described twice." if dup else
                     f'{u["x1"] - u["x0"]:.0f}x{u["y1"] - u["y0"]:.0f}pt image.',
                score=92 if dup else 40,
                confidence="high" if dup else "low"))

        # --- form fields ----------------------------------------------------
        elements.extend(_form_fields(page, pno, H, elements))

        # unmatched text objects paint nothing useful (Slides emits empty runs)
        for u in text_units:
            if not u.get("used"):
                elements.append(Element(
                    id=f"p{pno}x{len(elements)}", page=pno, kind="text",
                    role="Artifact", auto_role="Artifact", ops=[u["ops"]],
                    text="", note="Empty text run.", score=95,
                    confidence="high"))

        _order(elements, cols, H)
        _nest_lists(elements)
        _temper_scores(elements, cols, W)
        # A slide is a section, so every one of them gets its own title and its
        # own H1. A document flows: a section heading that happens to land at the
        # top of page 5 is still a section heading, and promoting it produced H1s
        # at 28pt, 18pt and 14pt in the same file. Only the first page of a
        # document can carry the document's title.
        _restore_page_title(elements, body_here, H, slides or pno == 0)
        _match_peers(elements)
        _demote_trailing_labels(elements)
        _demote_kickers(elements)
        pm.tables = _find_tables(doc[pno], elements, pno, H, lines)
        _order_tables(elements, pm.tables)
        _demote_table_cells(elements, pm.tables)
        pm.elements = elements
        model.pages.append(pm)

    # Text the page-level pass cannot reach: it sits inside a form XObject, so
    # there is no operator range on this page to wrap.  Left unreported, those
    # elements are silently dropped and the file looks tagged while saying
    # nothing, which is worse than refusing.
    # Form content is reachable now, so the only text still out of reach is inside
    # a form drawn more than once: its marked content would be referenced from two
    # places at once, which is not representable.
    shared = shared_form_pages(pdf)
    model.nested_text_pages = len(shared)

    _classify_images(model)
    # Classifying images can demote one to an artifact after ordering, which
    # would leave a hole in the reading-order numbers the user sees.
    for p in model.pages:
        renumber(p)

    # Saving through pikepdf writes a decrypted file, so any password or
    # permissions on the original are dropped. Silently losing a restriction
    # someone deliberately applied is not acceptable, so it gets reported.
    try:
        model.was_encrypted = bool(pdf.is_encrypted)
    except Exception:
        model.was_encrypted = False

    model.adopted_ours = tagged_by_this_app(pdf)
    model.adopted = _adopt_existing(pdf, model)

    model.unmappable = sum(1 for p in model.pages for e in p.elements
                           if e.kind == "text" and e.role != "Artifact"
                           and not e.ops and e.text.strip())

    if not model.title:
        h1 = next((e for p in model.pages for e in p.elements
                   if e.role == "H1" and e.text.strip()), None)
        model.title = h1.text.strip() if h1 else ""
    return model


def _body_size(lines):
    """Most common text size, weighted by how much text is set at it."""
    if not lines:
        return 11.0
    weights = {}
    for l in lines:
        weights[l["size"]] = weights.get(l["size"], 0) + max(len(l["text"]), 1)
    return max(weights.items(), key=lambda kv: kv[1])[0]


# A page needs this much text before its own dominant size is worth trusting
# over the document's.
_PAGE_BODY_LINES = 6
_PAGE_BODY_CHARS = 250


def _page_body_size(lines, doc_body):
    """The size to judge this page's headings against.

    A heading is bigger than *the text it introduces*, which is text on the same
    page, not a document-wide average. One number for the whole file breaks as
    soon as a file mixes scales: a deck with one dense small-print page had its
    body measured at 10pt while its slides were set at 14 to 20, so on those
    slides everything was half again the "body" size and 74% of the document came
    out as a heading.

    A sparse page cannot vote on its own size — a slide holding only a title
    would make that title the body text and lose the heading entirely — so those
    fall back to the document figure.

    The two are combined by taking the *larger*, because the damage only runs one
    way: too small a baseline invents headings, while too large a one merely
    misses a marginal case. A page carrying a lot of small print would otherwise
    set the bar at 9pt and turn every ordinary 13pt label on it into a heading.
    """
    text = [l for l in lines if (l.get("text") or "").strip()]
    if len(text) < _PAGE_BODY_LINES:
        return doc_body
    if sum(len(l["text"]) for l in text) < _PAGE_BODY_CHARS:
        return doc_body
    return max(_body_size(text), doc_body)


def _break_at(size, wrap):
    """How big a vertical gap starts a new block.

    Clamped so it can only ever be *tighter* than the old fixed multiple, never
    looser: where a page gives no clear answer the previous behaviour stands, so
    this can reduce over-merging but cannot introduce new fragmentation.
    """
    loose = size * 1.4
    if wrap is None:
        return loose
    return min(loose, wrap + max(1.2, size * 0.12))


def _wrap_gap(lines):
    """The vertical gap a genuine wrapped line leaves, measured on this page.

    A paragraph break has to be told from a line wrap, and the difference between
    them is a couple of points, not the fourteen that a fixed multiple of the font
    size allows. On a real document a wrap left 0.8pt while the next list item
    left 2.3pt, so eight list items merged into one paragraph. Leading varies by
    document (measured across files, the wrap gap runs 0.8pt to 2.2pt), so it is
    measured here rather than assumed.

    Sampled only between consecutive lines of the same size starting at the same
    x, which is what a left-aligned continuation looks like.
    """
    gaps = []
    prev = None
    for ln in lines:
        if prev is not None:
            same_size = abs(prev["size"] - ln["size"]) < 0.3
            same_left = abs(prev["bbox"][0] - ln["bbox"][0]) < 2
            if same_size and same_left:
                d = ln["bbox"][1] - prev["bbox"][3]
                # Slightly negative is normal: a tight leading makes consecutive
                # line boxes overlap by a fraction of a point. Discarding those
                # threw away exactly the samples that say what a wrap looks like
                # on a densely set page, leaving only the real breaks behind.
                if -3 <= d < 40:
                    gaps.append(max(0.0, d))
        prev = ln
    if len(gaps) < 4:
        return None
    gaps.sort()
    return gaps[len(gaps) // 5]           # 20th percentile: a typical wrap


def _group(lines, body, pno, cols, H, shapes=()):
    """Turn lines into elements, merging wrapped lines into one paragraph."""
    out = []
    cur = None
    last = None                      # bbox of the line cur last absorbed
    wrap = _wrap_gap(lines)          # what a real wrap looks like on this page
    # An asterisk is a footnote mark as often as it is a bullet, so it only counts
    # as one when the page has more than one of them: "* Loaded cost estimate of
    # $250K/year" at the foot of a section was coming through as a list item.
    stars = sum(1 for l in lines if l["text"].lstrip()[:1] == "*")

    def flush():
        nonlocal cur
        if cur is not None:
            cur.text = re.sub(r"\s+", " ", _visible(cur.text))
            # The role was proposed from the *first* line, before anyone knew how
            # much text would wrap into it. A paragraph's opening line is short,
            # so a paragraph set larger than the body came out as a heading and
            # then quietly absorbed another twenty words. Judge it again now the
            # whole thing is known.
            if _is_quotation(cur.text):
                cur.auto_role = cur.role = "BlockQuote"
                cur.score = 90
                cur.confidence = "high"
                cur.note = ("Opens and closes with quote marks, so it reads as a "
                            "quotation rather than the document's own words.")
            elif cur.role.startswith("H") and _is_prose(cur.text):
                words = len(cur.text.split())
                cur.auto_role = cur.role = "P"
                cur.score = 86
                cur.confidence = "high"
                cur.note = (f"Set larger than the body text, but {words} words "
                            "reads as a passage rather than a heading.")
            out.append(cur)
            cur = None

    for ln in lines:
        marker, rest, ordered = _strip_marker(ln["text"])
        if marker == "*" and stars < 2:
            marker, rest = "", ln["text"].strip()
        role, score, note = _propose(ln, body, marker, ordered)
        # A zero-width space is not whitespace to str.strip(), and Google Docs
        # puts one where a list item's text goes. Without this the bullet looked
        # like it already had its text, so the real words split off into their own
        # paragraph and every list came out as alternating empty bullets and
        # orphaned lines.
        bullet_only = marker and not _visible(rest)

        # A bullet glyph alone on a line belongs to the item that follows it.
        if bullet_only:
            flush()
            cur = Element(id=f"p{pno}e{len(out)}", page=pno, kind="text",
                          role="LI-ordered" if ordered else "LI",
                          auto_role="LI-ordered" if ordered else "LI",
                          text="", marker=marker, bbox=ln["bbox"], size=ln["size"],
                          font=ln["font"], bold=ln["bold"], ops=list(ln["ops"]),
                          score=95, confidence="high",
                          note="Starts with a list marker, so this is almost "
                               "certainly a list item.")
            cur.column = _column_of(ln["bbox"], cols)
            cur.parts = [{"text": "", "bbox": list(ln["bbox"]),
                          "ops": [list(o) for o in ln["ops"]]}]
            cur.indent = round(ln["bbox"][0], 1)
            last = ln["bbox"]
            continue

        # Decide continuation purely from geometry and size.  Deriving it from
        # the *proposed role* instead used to split a wrapped heading in two.
        # Distance from the bottom of what we have to the top of the next line.
        # This was the other way round, which for a line below is always negative,
        # so the paragraph-break test never fired: any two pieces of the same size
        # in the same column merged however far apart they sat, and a slide title
        # could absorb a callout halfway down the page.
        gap = (ln["bbox"][1] - cur.bbox[3]) if cur else 0
        same_col = cur is not None and _column_of(ln["bbox"], cols) == cur.column
        awaiting_text = (cur is not None and cur.role in ("LI", "LI-ordered")
                         and not _visible(cur.text))
        # A wrapped line always sits *below* the line it continues. Two lines
        # sharing a baseline are side by side, so they are separate cells or
        # columns and never a continuation.
        #
        # This does not depend on column detection, which is a guess and cannot
        # find a boundary that no line starts at: on a real table it left "A" and
        # "8" in one column and merged them into a single item reading "A 8".
        # Overlap on the baseline is a fact about the page, so it holds regardless.
        # A bullet glyph and the words it belongs to sit side by side by
        # definition, so the rule below must not separate them: doing that left
        # every list item as an empty bullet with its text stranded beside it.
        side_by_side = False
        if cur is not None and last is not None and not awaiting_text:
            ov = min(last[3], ln["bbox"][3]) - max(last[1], ln["bbox"][1])
            side_by_side = ov > 0.5 * min(last[3] - last[1],
                                          ln["bbox"][3] - ln["bbox"][1])
        starts_new = (
            cur is None
            or not same_col
            or (abs(cur.size - ln["size"]) > 0.6 and not awaiting_text)
            or gap > _break_at(ln["size"], wrap)            # paragraph break
            # Text flows down. A line that starts above the element it would join
            # is not a continuation of it, whatever the content stream's order:
            # a step number drawn in the margin *after* its paragraph was merging
            # into that paragraph, which then dragged the paragraph's box up above
            # its own heading and reversed the two in the reading order.
            or (cur is not None and not awaiting_text
                and ln["bbox"][1] < cur.bbox[1] - 2)
            or (marker and cur.text.strip())                # next list item
            or (marker and cur.role not in ("LI", "LI-ordered"))
            or side_by_side
            # Two cards side by side each hold a sentence. Without this the
            # sentences ran together into one paragraph, because they are the
            # same size and sit at much the same height.
            or (cur is not None and not awaiting_text
                and _in_different_shapes(cur.bbox, ln["bbox"], shapes))
        )
        if starts_new:
            flush()
            cur = Element(id=f"p{pno}e{len(out)}", page=pno, kind="text",
                          role=role, auto_role=role, text=rest, marker=marker,
                          bbox=ln["bbox"], size=ln["size"], font=ln["font"],
                          bold=ln["bold"], ops=list(ln["ops"]),
                          score=score, confidence=_score_to_band(score), note=note)
            cur.column = _column_of(ln["bbox"], cols)
            cur.parts = [{"text": rest, "bbox": list(ln["bbox"]),
                          "ops": [list(o) for o in ln["ops"]]}]
            if marker:
                cur.indent = round(ln["bbox"][0], 1)
        else:
            if awaiting_text:
                cur.size = ln["size"]
                cur.font = ln["font"]
            cur.text = (cur.text + " " + rest) if cur.text else rest
            cur.ops.extend(ln["ops"])
            cur.parts.append({"text": rest, "bbox": list(ln["bbox"]),
                              "ops": [list(o) for o in ln["ops"]]})
            x0, y0, x1, y1 = cur.bbox
            b = ln["bbox"]
            cur.bbox = (min(x0, b[0]), min(y0, b[1]), max(x1, b[2]), max(y1, b[3]))
        last = ln["bbox"]
    flush()
    return out


# Size ratios that separate heading levels from body text.  Kept explicit because
# the *distance* from a boundary is what decides how confident a proposal is: a
# line 60% bigger than the body text is obviously a heading, one 13% bigger is a
# guess that deserves a human glance.
_H_BOUNDS = ((1.90, "H1"), (1.45, "H2"), (1.12, "H3"))

# A heading is a label for what follows, so length is a hard limit rather than a
# penalty. Size alone cannot tell a heading from large-set prose: a deck routinely
# sets a whole paragraph two sizes up, and calling that a heading puts the entire
# paragraph into the list a screen reader user navigates by.
_HEAD_MAX_WORDS = 12


def _is_figure(text):
    """Whether this is a bare figure rather than a heading.

    Display numbers are set large and bold exactly like headings — "4X", "$110M",
    "100+", "2021" — but a number is not a label for the content that follows it,
    and a screen reader user pulling up the heading list gets a column of digits
    with nothing to tell them apart. Requires one run of two or more letters, so
    a short real label like "QA" still counts as a heading.
    """
    return not re.search(r"[^\W\d_]{2,}", text or "")


# Every flavour of quote mark a slide deck might use, straight or curly.
_QUOTE_OPEN = "\"'\u201c\u2018\u00ab\u201e"
_QUOTE_CLOSE = "\"'\u201d\u2019\u00bb\u201c"


def _is_quotation(text):
    """Whether this is a quotation, by its own punctuation.

    Tagged as a quotation a screen reader can say so, and a reader can tell the
    difference between the document's own words and someone else's. Left as an
    ordinary paragraph, a testimonial reads as if the document is asserting it.

    Deliberately narrow: opens and closes with a quote mark, and long enough to be
    a passage rather than a quoted term. Detection has to run on the assembled
    item, since the closing mark is on the last line.
    """
    t = (text or "").strip()
    if len(t.split()) < 4:
        return False
    if t[0] not in _QUOTE_OPEN:
        return False
    # Allow an attribution dash or a full stop after the closing mark.
    tail = t.rstrip(" .\u2014\u2013-")
    return bool(tail) and tail[-1] in _QUOTE_CLOSE


def _is_prose(text):
    """Whether this reads as running text rather than a label."""
    t = (text or "").strip()
    if len(t.split()) > _HEAD_MAX_WORDS:
        return True
    # More than one sentence is prose whatever its length. A single trailing full
    # stop is not enough on its own, because plenty of real headings carry one.
    return len(re.findall(r"[.!?][\"'\u201d\u2019)\]]?(?:\s|$)", t)) > 1


def _score_to_band(score):
    if score >= 80:
        return "high"
    return "medium" if score >= 55 else "low"


def _propose(ln, body, marker, ordered):
    """Propose a role, with a 0-100 confidence and the reason in plain English.

    The score is not decoration: it drives which items the user is asked to look
    at, so it has to reflect the actual strength of the evidence rather than a
    flat guess per category.
    """
    size, bold = ln["size"], ln["bold"]
    words = len(ln["text"].split())
    ratio = size / body if body else 1.0

    if marker:
        return ("LI-ordered" if ordered else "LI"), 95, \
            "Starts with a list marker, so this is almost certainly a list item."

    prose = _is_prose(ln["text"])
    if _is_figure(ln["text"]) and ratio >= _H_BOUNDS[-1][0]:
        return "P", 84, (f"{size:g}pt against {body:g}pt body text, so it is set "
                         "large, but it is a figure rather than a label, so it "
                         "reads as text.")
    for bound, role in _H_BOUNDS:
        if ratio >= bound:
            if prose:
                return "P", 86, (
                    f"{size:g}pt against {body:g}pt body text, so it is set larger, "
                    f"but {words} words reads as a passage rather than a heading.")
            # How far clear of the boundary is it? Right on the line is a coin
            # toss; comfortably above it is not.
            margin = (ratio - bound) / bound
            score = 68 + min(27, margin * 220)
            why = f"{size:g}pt against {body:g}pt body text"
            if margin < 0.05:
                why += ", which is only just over the line for this level"
            if words > 15:
                score -= 22
                why += f", but {words} words is long for a heading"
            elif words > 25:
                score -= 32
            if bold:
                score += 4
                why += ", and it is bold"
            return role, max(20, min(98, round(score))), why + "."

    if bold and words <= _HEAD_MAX_WORDS and not prose \
            and not _is_figure(ln["text"]) and ratio >= 0.85:
        return "H3", 62, (f"Bold and only {words} word{'s' if words != 1 else ''}, "
                          "which usually means a label or subheading, but it is not "
                          "larger than the body text.")

    # Weight by what a wrong call actually costs. Reading a caption as a paragraph
    # changes almost nothing for a screen reader, so an ambiguous small-text call
    # does not deserve to crowd out the decisions that do matter.
    if ratio <= 0.80:
        return "P", 76, (f"{size:g}pt against {body:g}pt body text, so noticeably "
                         "smaller. It reads as text either way, but it might be a "
                         "caption or a footnote.")
    if ratio < 0.98:
        return "P", 88, (f"{size:g}pt against {body:g}pt body text, near enough the "
                         "body size.")
    return "P", 95, "Set at the body text size."


def _order(elements, cols, H):
    """Reading order: column by column, top to bottom inside each column.

    Things sitting on the same line have to tie on the vertical so that the
    horizontal decides between them, which is how a marker in the margin comes
    before the text it belongs to. Rounding the top to a fixed grid does that only
    by luck: a step number centred against its heading sat two points above it, and
    whether the pair landed in the same bucket depended on which side of a
    four-point boundary they happened to fall. So the rows are clustered with a
    tolerance instead, and the tolerance comes from the text's own size.
    """
    for e in elements:
        e.column = _column_of(e.bbox, cols)
    live = [e for e in elements if e.role != "Artifact"]
    bands = {}
    for col in {e.column for e in live}:
        rows, band_top, band_id = [], None, 0
        for e in sorted((x for x in live if x.column == col),
                        key=lambda x: (x.bbox[1], x.bbox[0])):
            tol = max(6.0, (e.size or 10) * 0.7)
            if band_top is None or e.bbox[1] - band_top > tol:
                band_id += 1
                band_top = e.bbox[1]
            bands[e.id] = band_id
    def key(e):
        return (e.column, bands.get(e.id, 0), e.bbox[0])
    ordered = sorted(live, key=key)
    for i, e in enumerate(ordered):
        e.order = i
    for e in elements:
        if e.role == "Artifact":
            e.order = 10_000
    return elements


def normalise_headings(model):
    """Assign heading levels so the sequence never skips a level.

    Ranking font sizes globally is not enough: a deck can use tiers 1 and 5 on
    one slide, which reads as H1 followed by H5 and fails PDF/UA 7.4.2.  Walking
    the document with a stack of open sizes produces contiguous levels by
    construction, which is the same trick outline generators use.
    """
    # Anything already settled — adopted from the file's own tags, or confirmed by
    # the user — is left exactly as it is. Re-levelling it would quietly overrule
    # a decision someone made on purpose; if that leaves a gap, heading_warnings
    # tells them rather than the app deciding for them.
    seq = [e for p in model.pages
           for e in sorted(p.elements, key=lambda x: x.order)
           if e.role.startswith("H") and e.size and not e.reviewed]
    stack = []                       # sizes of the currently open levels
    titled_page = None               # page whose own title is the H1
    for e in seq:
        # A page's own title starts a new section, so the levels restart there.
        # Without this the stack carries sizes across the whole file and the same
        # kind of thing gets a different level depending on where it appears: in
        # one deck the slide titles came out H1 on the first seven slides and H3
        # on the rest, purely because a slightly larger size had been seen
        # earlier. Titles are only marked where the biggest text sits at the top
        # of the page, so a continuous document without per-page headings is
        # unaffected.
        if getattr(e, "is_title", False):
            stack = []
            titled_page = e.page
        s = round(e.size, 1)
        while stack and stack[-1] < s - 0.05:
            stack.pop()
        if not stack or stack[-1] > s + 0.05:
            stack.append(s)
        lvl = min(len(stack), 6)
        # A page has one top-level heading: its own title. Anything else on the
        # page sits under it, however large it is set. Two display words set at
        # the title's own size were otherwise coming out as further H1s, so a
        # slide read as three level-one headings.
        if titled_page == e.page and not getattr(e, "is_title", False):
            lvl = max(lvl, 2)
        new = f"H{lvl}"
        if new != e.role:
            e.note = (e.note + f" Set to {new} so heading levels run in order.").strip()
        e.role = new
        e.auto_role = new
    return model


def heading_warnings(model):
    """Report skipped heading levels in whatever the user has currently chosen."""
    seq = [e for p in model.pages for e in sorted(p.elements, key=lambda x: (x.page, x.order))
           if e.role.startswith("H")]
    out = []
    prev = 0
    for e in seq:
        lvl = int(e.role[1])
        if prev and lvl > prev + 1:
            out.append({"id": e.id, "page": e.page,
                        "message": f"H{prev} jumps straight to H{lvl} — a level is skipped.",
                        "text": e.text[:60]})
        prev = lvl
    if seq and int(seq[0].role[1]) != 1:
        out.insert(0, {"id": seq[0].id, "page": seq[0].page,
                       "message": f"The first heading is {seq[0].role}, not H1.",
                       "text": seq[0].text[:60]})
    return out


# A hairline is thin in one direction; an icon is small in both. Judged on real
# dimensions rather than share of the page: a wall of brand logos on a slide sits
# at well under half a percent of it each, and an area test quietly marked seven of
# seventeen as decoration, so they never appeared for anyone to describe.
_RULE_THICKNESS = 10
_ICON_SIDE = 24


def _is_furniture(bbox):
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    return min(w, h) < _RULE_THICKNESS or (w < _ICON_SIDE and h < _ICON_SIDE)


def _form_fields(page, pno, H, elements):
    """Widget annotations, as elements needing a label.

    A form field is the one thing on a page that tagging alone cannot rescue: PDF/UA
    wants each widget nested in a /Form element *and* carrying a /TU tooltip, and
    the tooltip is the field's accessible name. Nothing in the file says what that
    name is, so the visible label beside the field is proposed and the user
    confirms it, exactly as they do for an image description.
    """
    out = []
    annots = page.get("/Annots")
    if not annots:
        return out
    for i, annot in enumerate(annots):
        try:
            if str(annot.get("/Subtype", "")) != "/Widget":
                continue
            if int(annot.get("/F", 0) or 0) & 2:          # hidden
                continue
            rect = [float(v) for v in annot.get("/Rect", [0, 0, 0, 0])]
        except Exception:
            continue
        x0, y0 = min(rect[0], rect[2]), min(rect[1], rect[3])
        x1, y1 = max(rect[0], rect[2]), max(rect[1], rect[3])
        bbox = (x0, H - y1, x1, H - y0)                   # into pymupdf space
        existing = str(annot.get("/TU", "") or "")
        label = existing or _label_for(bbox, elements)
        out.append(Element(
            id=f"p{pno}f{i}", page=pno, kind="field", role="Form",
            auto_role="Form", bbox=bbox, ops=[], text="(form field)",
            alt=label,
            score=95 if existing else (60 if label else 30),
            confidence="high" if existing else ("medium" if label else "low"),
            reviewed=bool(existing),
            note=("The file already gives this field a name."
                  if existing else
                  ("Proposed from the text beside it. A screen reader reads this "
                   "out as the field's name, so it is worth checking."
                   if label else
                   "This field has no name at all. Someone filling the form with a "
                   "screen reader hears nothing but 'edit text'."))))
    return out


def _label_for(bbox, elements):
    """The visible text most likely to be a field's label: to its left, or above."""
    x0, y0, x1, y1 = bbox
    best, best_d = "", 1e9
    for e in elements:
        if e.kind != "text" or not (e.text or "").strip():
            continue
        ex0, ey0, ex1, ey1 = e.bbox
        vertical_overlap = min(y1, ey1) - max(y0, ey0)
        if vertical_overlap > 0 and ex1 <= x0 + 4:            # to the left
            d = x0 - ex1
        elif ey1 <= y0 + 4 and abs(ex0 - x0) < 60:            # directly above
            d = (y0 - ey1) + 20                                # prefer left labels
        else:
            continue
        if 0 <= d < best_d and d < 120:
            best, best_d = re.sub(r"[\s:*]+$", "", (e.text or "").strip()), d
    return best


def _classify_images(model):
    """Separate meaningful images from template furniture.

    An image that lands in the same place on several pages is almost always a
    logo, watermark or slide-master decoration.  Tagging those as figures forces
    someone to write alt text for the same logo once per page, and the honest
    description is "nothing" — so they become artifacts instead.
    """
    n = len(model.pages) or 1
    sigs = {}
    for p in model.pages:
        for e in p.elements:
            if e.kind != "image" or "second time" in e.note:
                continue
            x0, y0, x1, y1 = e.bbox
            sig = (round(x0 / 4), round(y0 / 4), round((x1 - x0) / 4), round((y1 - y0) / 4))
            e.note = e.note
            sigs.setdefault(sig, []).append(e)

    for sig, group in sigs.items():
        pages = {e.page for e in group}
        repeated = len(pages) >= 3 or (n >= 2 and len(pages) >= max(2, n * 0.6))
        for e in group:
            page = model.pages[e.page]
            area = (e.bbox[2] - e.bbox[0]) * (e.bbox[3] - e.bbox[1])
            frac = area / (page.width * page.height or 1)
            if repeated:
                e.role = e.auto_role = "Artifact"
                e.score, e.confidence = 88, "high"
                e.note = (f"Appears in the same spot on {len(pages)} pages, so it "
                          "reads as slide furniture rather than content.")
            elif _is_furniture(e.bbox):
                e.role = e.auto_role = "Artifact"
                e.score, e.confidence = 66, "medium"
                e.note = ("Very small, so probably an icon or a rule. Worth a look "
                          "if it is the only thing carrying a piece of meaning.")
            elif frac > 0.55:
                e.role = e.auto_role = "Figure"
                e.score, e.confidence = 30, "low"
                e.note = ("Covers most of the page, so it is carrying the slide. "
                          "If it is a picture, describe what it shows; if it is a "
                          "screenshot of text, the words need to be in the "
                          "description.")
            else:
                e.role = e.auto_role = "Figure"
                e.score, e.confidence = 40, "low"
                e.note = ("Only you can say whether this carries meaning. Describe "
                          "it, or mark it decorative to skip it.")


def _nest_lists(elements):
    """Turn marker indents into list nesting depths, one list at a time.

    Depth has to be judged per list, not per page: a second list further across
    the slide is a new list starting at level 1, not level 3 of the first one.
    A run of items broken by a heading or a paragraph starts a new list.
    """
    live = sorted([e for e in elements if e.role != "Artifact"],
                  key=lambda e: e.order)
    run = []

    def close(run):
        items = [e for e in run if e.indent]
        if not items:
            return
        stops = []
        for e in items:
            if not any(abs(e.indent - s) <= 6 for s in stops):
                stops.append(e.indent)
        stops.sort()
        for e in items:
            e.level = min(range(len(stops)),
                          key=lambda i: abs(stops[i] - e.indent)) + 1

    for e in live:
        if e.role in ("LI", "LI-ordered"):
            run.append(e)
        else:
            close(run)
            run = []
    close(run)


def renumber(page):
    """Reassign sequential reading-order numbers, artifacts last."""
    live = sorted([e for e in page.elements if e.role != "Artifact"],
                  key=lambda e: e.order)
    for i, e in enumerate(live):
        e.order = i
    for e in page.elements:
        if e.role == "Artifact":
            e.order = 10_000


def _temper_scores(elements, cols, page_width):
    """Lower confidence where the *layout* was ambiguous, not just the role.

    A role can be well evidenced while its position is not. An element sitting
    between two columns has a reading-order placement that is a guess, and
    reading order is the one thing no conformance checker will ever question.
    """
    edges = (cols or {}).get("edges") or []
    for e in elements:
        if e.role == "Artifact":
            continue
        if len(edges) > 1:
            x = e.bbox[0]
            nearest = min(edges, key=lambda v: abs(v - x))
            others = [v for v in edges if v != nearest]
            if others:
                second = min(others, key=lambda v: abs(v - x))
                gap = abs(second - nearest) or 1
                # 0 = sitting on a column edge, 0.5 = midway between two
                offness = min(abs(x - nearest) / gap, 0.5)
                if offness > 0.35:
                    e.score = max(20, e.score - 10)
                    e.note = (e.note + " It also sits between two columns, so where "
                              "it falls in the reading order is a guess.").strip()
        # A very wide element in a multi-column layout probably spans them, which
        # makes its column assignment meaningless.
        if len(edges) > 1 and (e.bbox[2] - e.bbox[0]) > page_width * 0.72:
            e.score = max(20, e.score - 5)
        e.confidence = _score_to_band(e.score)


def shared_form_pages(pdf):
    """Pages whose text sits in a form XObject that is drawn more than once.

    A form's marked content belongs to that one stream, so a form drawn twice
    cannot carry two different sets of structure. Uniquifying it means rewriting
    the invoking stream's resource names, which is more surgery than the case
    warrants: it does not occur anywhere in the test corpus. Detected and reported
    instead, exactly as before.
    """
    counts = {}
    where = {}
    for pno, page in enumerate(pdf.pages):
        xo = (page.get("/Resources") or {}).get("/XObject", {}) or {}
        for _nm, v in xo.items():
            if str(v.get("/Subtype", "")) != "/Form":
                continue
            try:
                key = v.objgen
            except Exception:
                continue
            counts[key] = counts.get(key, 0) + 1
            where.setdefault(key, set()).add(pno)
    out = set()
    for key, n in counts.items():
        if n > 1:
            try:
                text, _marked = form_contents(pdf.get_object(key))
            except Exception:
                text = False
            if text:
                out |= where[key]
    return sorted(out)


# --------------------------------------------------------------------------- #
# tables
# --------------------------------------------------------------------------- #
# The grid itself comes from the PDF library; what it cannot do is tell a real
# table from any other arrangement that happens to line up. These two numbers do
# most of that: a real table fills most of its cells and has a populated first
# row, while a diagram whose labels happen to form a grid leaves it mostly empty.
TABLE_ACCEPT_FILL = 0.5
TABLE_ACCEPT_HEADER = 0.6
TABLE_ASK_FILL = 0.3


def _table_signals(table):
    data = table.extract()
    if not data:
        return 0.0, 0.0
    cells = [c for row in data for c in row]
    filled = [c for c in cells if c and str(c).strip()]
    fill = len(filled) / max(len(cells), 1)
    head = data[0]
    hdr = sum(1 for c in head if c and str(c).strip()) / max(len(head), 1)
    return fill, hdr


def _restore_page_title(elements, body, page_height, allow):
    """Give a page back its title when the length rule has just taken it away.

    A slide title is often a full sentence — "Our exhaustive checkpoint depth
    focuses on issues that are the hardest to detect..." is 22 words — and the
    prose rule is right to distrust length in general but wrong here. Position and
    prominence say what length cannot. Slide titles are how a screen reader user
    moves through a deck, so losing them is worse than the occasional long
    heading.
    """
    if not allow:
        return
    text = [e for e in elements
            if e.kind == "text" and e.role != "Artifact" and (e.text or "").strip()]
    if not text:
        return
    top = min(e.bbox[1] for e in text)
    band = [e for e in text if e.bbox[1] <= top + page_height * 0.08]
    if not band:
        return
    # The most prominent thing in the top band, not the biggest thing on the
    # slide. A display word set mid-page can easily be larger — one slide had
    # "Axe" and "Engines" at 24pt above a 20pt title — and picking the biggest
    # meant no title was found, so the slide came out with three H1s.
    head = max(band, key=lambda e: (e.size, -e.bbox[1]))
    if not body or head.size < body * 1.25:
        return                       # nothing at the top stands out enough
    head.is_title = True
    if not head.role.startswith("H"):
        head.auto_role = head.role = "H1"
        head.score = 84
        head.confidence = "high"
        head.note = ("The largest text at the top of the page, so it reads as the "
                     "title even though it is long for a heading.")


def _match_peers(elements):
    """Text set the same way on a page should be tagged the same way.

    Three cards side by side, all the same size and weight, came out as one
    heading and two paragraphs purely because the first happened to be short
    enough to pass the length test while the others were not. Whatever the right
    answer is, it is the same for all three, and body copy is the safer guess: a
    missed heading costs a navigation shortcut, an invented one puts a sentence
    into the list a screen reader user navigates by.

    A page title is exempt: it is identified by position, not size, and may well
    share its size with something else on the page.
    """
    groups = {}
    for e in elements:
        if e.kind != "text" or e.role == "Artifact" or not (e.text or "").strip():
            continue
        if getattr(e, "is_title", False) or e.reviewed:
            continue
        groups.setdefault((round(e.size, 1), bool(e.bold)), []).append(e)
    for (size, _bold), peers in groups.items():
        if len(peers) < 2:
            continue
        heads = [e for e in peers if e.role.startswith("H")]
        prose = [e for e in peers if e.role == "P" and _is_prose(e.text)]
        if not heads or not prose:
            continue
        for e in heads:
            e.auto_role = e.role = "P"
            e.score = 82
            e.confidence = "high"
            e.note = (f"Other text on this page is set the same way ({size:g}pt) "
                      "and reads as body copy, so this does too.")


def _demote_trailing_labels(elements):
    """A heading introduces what comes after it, not what came before.

    A figure with its caption under it — "2021" then "Launch Year", "$110M" then
    "Capital Raised" — put the label after the content and tagged it as a heading,
    so the structure read as a paragraph followed by its own heading. Whatever that
    label is, it is not a heading for the text above it.

    Only fires where the item directly above is *larger* and not itself a heading,
    which is the caption shape. A genuine subheading is bigger than its
    surroundings and comes first, so it is untouched.
    """
    live = sorted([e for e in elements
                   if e.role != "Artifact" and e.kind == "text" and (e.text or "").strip()],
                  key=lambda e: e.order)
    for prev, e in zip(live, live[1:]):
        if not e.role.startswith("H") or e.reviewed or getattr(e, "is_title", False):
            continue
        if prev.role.startswith("H") or prev.size <= e.size + 0.05:
            continue
        # Has to be the same block of content, not just the previous item.
        if e.bbox[1] - prev.bbox[3] > e.size * 1.6:
            continue
        e.auto_role = e.role = "P"
        e.score = 84
        e.confidence = "high"
        e.note = ("Sits under the larger text it belongs to, so it reads as a "
                  "label for it rather than a heading of its own.")


def _demote_kickers(elements):
    """A small label directly above a larger heading is a kicker, not a heading.

    Documents often set a short eyebrow over the real heading &mdash; "THE SCOPE"
    above "What You're Actually Building". Both are bold and both clear the size
    bar, so both came out as headings, and the smaller one came first: the heading
    list read "THE SCOPE, What You're Actually Building" at the same level, with
    the label announced as though it introduced something.
    """
    live = sorted([e for e in elements
                   if e.role != "Artifact" and e.kind == "text" and (e.text or "").strip()],
                  key=lambda e: e.order)
    for e, nxt in zip(live, live[1:]):
        if not e.role.startswith("H") or e.reviewed or getattr(e, "is_title", False):
            continue
        if not nxt.role.startswith("H"):
            continue
        if nxt.size <= e.size + 0.05:
            continue                     # the one after is not the bigger one
        # A kicker sits directly above its heading, so the gap has to be small AND
        # the right way round. Testing only the upper bound let a label at the
        # bottom of one column pair with a heading at the top of the next, where
        # the gap is negative and sails under any ceiling.
        gap = nxt.bbox[1] - e.bbox[3]
        if not -e.size * 0.5 <= gap <= e.size * 1.6:
            continue                     # not stacked one above the other
        if e.bbox[0] >= nxt.bbox[2] or nxt.bbox[0] >= e.bbox[2]:
            continue                     # no horizontal overlap, so not a pair
        e.auto_role = e.role = "P"
        e.score = 82
        e.confidence = "high"
        e.note = ("A short label sitting above the larger heading it introduces, "
                  "so it reads as a kicker rather than a heading of its own.")


def _order_tables(elements, tables):
    """A table reads across each row, then down to the next one.

    Page reading order is column by column, which is right for a two-column
    layout and wrong inside a table: it walks the whole first column of cells,
    then the whole second, so a row's cells are read nowhere near each other and
    a value never arrives next to the row it belongs to.

    Sequenced from the table's own grid rather than from geometry, so merged
    cells land once, in the row they start in. The table keeps the same slots in
    the page's order, so where it sits in the document does not move.
    """
    by_id = {e.id: e for e in elements}
    for t in tables:
        if not t.accepted:
            continue
        seq, seen = [], set()
        for c in sorted(t.cells, key=lambda c: (c.row, c.col)):
            for eid in c.elements:
                e = by_id.get(eid)
                if e is not None and e.id not in seen:
                    seen.add(e.id)
                    seq.append(e)
        if len(seq) < 2:
            continue
        slots = sorted(e.order for e in seq)
        for e, slot in zip(seq, slots):
            e.order = slot


def _demote_table_cells(elements, tables):
    """Cell contents are not headings.

    A table already says which cells are headers, through /TH and /Scope, and that
    is what a screen reader announces. Tagging the cells as headings on top of
    that puts every cell into the heading list as well: one real table turned 34
    values, several of them single digits, into H4s and H5s. Bold, larger type in
    a header row is how a table is drawn, not evidence of a document heading.
    """
    claimed = {eid for t in tables if t.accepted for c in t.cells for eid in c.elements}
    for e in elements:
        if e.id in claimed and e.role.startswith("H"):
            e.auto_role = e.role = "P"
            e.score = 88
            e.confidence = "high"
            e.note = ("Inside a table, so it is a cell rather than a heading. The "
                      "table itself says which cells are headers.")


def _grid_lines(rows, axis):
    """The distinct edges of the table's grid, along x for columns or y for rows."""
    vals = set()
    for row in rows:
        for cell in row.cells:
            if cell is None:
                continue
            a, b = (cell[0], cell[2]) if axis == "col" else (cell[1], cell[3])
            vals.add(round(a, 1))
            vals.add(round(b, 1))
    return sorted(vals)


def _span(lines, lo, hi):
    """How many grid slots the range lo..hi covers.

    Measured against the grid's own edges rather than counted from the gaps left
    in the cell array. A gap only says a position is covered, not by which
    neighbour: the empty square left of a tall cell and below a wide one is
    claimed by both if you count runs, and two cells claiming one square is a
    conformance failure (7.2, "a table cell shall not have intersection with
    other cells"). The geometry is unambiguous, so it is what gets asked.
    """
    return sum(1 for v in lines if lo + 0.5 < v < hi - 0.5) + 1


def _rows_consistent(cells, cols):
    """Whether every row accounts for the full column count once spans are counted.

    PDF/UA requires it (7.2, "table rows shall have the same number of columns"),
    and a grid whose geometry cannot produce it is not a table we can describe:
    better to leave the text as ordinary paragraphs than to emit a table that is
    wrong. Some layout grids simply do not resolve — rows divided differently from
    one another — and those are exactly the ones least likely to be real tables.
    """
    if not cells:
        return False
    rows = {}
    for c in cells:
        rows.setdefault(c.row, []).append(c)
    pending = []
    for r in sorted(rows):
        width = sum(cs for _n, cs in pending) + sum(c.colspan for c in rows[r])
        if width != cols:
            return False
        pending = [(n - 1, cs) for n, cs in pending if n - 1 > 0]
        pending += [(c.rowspan - 1, c.colspan) for c in rows[r] if c.rowspan > 1]
    return True


# A borderless table drawn as one filled rectangle per cell. The columns come from
# the drawn geometry rather than from guessing at where text lines up, which is why
# this is safe where inferring columns from text alignment was not: a band only
# counts when the same x-range is painted several times down the page.
_BAND_MIN_REPEATS = 3
_BAND_MIN_WIDTH = 20
_SHAPE_TABLE_MIN_FILL = 0.55
# Columns of one table abut; separate cards have a gutter between them. Measured:
# a real shaded table tiled with gaps of 0, while two side-by-side comparison cards
# sat 16 and 20 points apart. Without this the pair of cards came out as a
# two-column table and reading across a row paired unrelated lines.
_BAND_MAX_GUTTER = 3


def _column_bands(page, W, H):
    """x-ranges painted repeatedly down the page: the columns of a shaded table."""
    seen = {}
    for dr in page.get_drawings():
        r = dr.get("rect")
        if not dr.get("fill") or r is None:
            continue
        if r.width < _BAND_MIN_WIDTH or r.height < 10:
            continue
        if r.width * r.height > 0.5 * W * H:
            continue                       # a page background, not a cell
        key = (round(r.x0), round(r.x1))
        seen.setdefault(key, []).append((r.y0, r.y1))
    bands = [k for k, v in seen.items() if len(v) >= _BAND_MIN_REPEATS]
    bands.sort()
    # Keep only bands that tile: a run of adjacent, non-overlapping ranges. A pair
    # of unrelated repeated shapes is not a table.
    run = []
    for b in bands:
        if run and b[0] < run[-1][1] - 2:
            continue                       # overlaps the previous band
        if run and b[0] - run[-1][1] > _BAND_MAX_GUTTER:
            if len(run) >= 2:
                break                      # a gap ends the table
            run = []
        run.append(b)
    return run if len(run) >= 2 else []


def _rows_from_text(lines, bands, top, bottom):
    """Group text lines into rows, using the first column to say where rows start.

    Wrapped lines inside a cell and the start of the next row look identical to a
    naive baseline grouping, so the two are told apart by distance: measured on a
    real table the wrap inside a cell was 17pt and the step to the next row was 34
    to 50pt, which separates cleanly. The first column carries the longest text and
    is always populated, so it is the one that defines where a row begins.
    """
    inside = []
    for ln in lines:
        x0, y0, x1, y1 = ln["bbox"]
        if not (top - 6 <= y0 and y1 <= bottom + 8):
            continue
        cx = (x0 + x1) / 2
        col = next((i for i, (bx0, bx1) in enumerate(bands)
                    if bx0 - 2 <= cx <= bx1 + 2), None)
        if col is None:
            continue
        inside.append((y0, y1, col, ln))
    if not inside:
        return []
    inside.sort(key=lambda t: (t[0], t[2]))

    firsts = sorted(y0 for y0, _y1, col, _ln in inside if col == 0)
    if len(firsts) < 2:
        return []
    gaps = sorted(b - a for a, b in zip(firsts, firsts[1:]))
    if not gaps:
        return []
    # Split the gaps into "wrap" and "new row" at the widest jump between them.
    jump, cut = 0, gaps[-1] + 1
    for a, b in zip(gaps, gaps[1:]):
        if b - a > jump:
            jump, cut = b - a, (a + b) / 2
    starts = [firsts[0]]
    for a, b in zip(firsts, firsts[1:]):
        if b - a >= cut:
            starts.append(b)
    if len(starts) < 2:
        return []

    rows = [[] for _ in starts]
    for y0, _y1, col, ln in inside:
        idx = 0
        for i, sy in enumerate(starts):
            if y0 >= sy - 6:
                idx = i
        rows[idx].append((y0, col, ln))
    return [r for r in rows if r]


def _shape_table(page, lines, pno, W, H):
    """A table whose cells are painted rather than ruled."""
    bands = _column_bands(page, W, H)
    if not bands:
        return None
    ys = []
    for dr in page.get_drawings():
        r = dr.get("rect")
        if not dr.get("fill") or r is None:
            continue
        if (round(r.x0), round(r.x1)) in bands:
            ys.append((r.y0, r.y1))
    if not ys:
        return None
    top, bottom = min(y for y, _ in ys), max(y for _, y in ys)
    rows = _rows_from_text(lines, bands, top, bottom)
    if len(rows) < 2:
        return None
    filled = sum(1 for r in rows for _y, _c, _l in r)
    if filled < len(rows) * len(bands) * _SHAPE_TABLE_MIN_FILL:
        return None
    return {"bands": bands, "rows": rows, "bbox": (bands[0][0], top,
                                                   bands[-1][1], bottom)}


def _find_tables(page, elements, pno, H, lines=()):
    """Propose tables, and claim the elements whose text sits in their cells."""
    try:
        found = page.find_tables().tables
    except Exception:
        found = []
    out = []
    taken = set()
    for ti, t in enumerate(found):
        rows = t.rows
        if len(rows) < 2 or t.col_count < 2:
            continue
        fill, hdr = _table_signals(t)
        if fill < TABLE_ASK_FILL:
            continue                       # a grid-shaped coincidence, not a table
        xs, ys = _grid_lines(rows, "col"), _grid_lines(rows, "row")
        cells, used = [], []
        for r, row in enumerate(rows):
            for c, rect in enumerate(row.cells):
                if rect is None:
                    continue               # covered by a span from above or left
                x0, y0, x1, y1 = rect
                ids, preview = [], []
                for e in elements:
                    if e.id in taken or e.kind != "text" or e.role == "Artifact":
                        continue
                    cx = (e.bbox[0] + e.bbox[2]) / 2
                    cy = (e.bbox[1] + e.bbox[3]) / 2
                    if x0 - 2 <= cx <= x1 + 2 and y0 - 2 <= cy <= y1 + 2:
                        ids.append(e.id)
                        preview.append(e.text)
                if ids:
                    used.extend(ids)
                cells.append(Cell(row=r, col=c,
                                  bbox=(x0, y0, x1, y1),
                                  rowspan=_span(ys, y0, y1),
                                  colspan=_span(xs, x0, x1),
                                  header=(r == 0),
                                  elements=ids,
                                  preview=" ".join(preview)[:60]))
        if not used:
            continue
        taken.update(used)
        if fill >= TABLE_ACCEPT_FILL and hdr >= TABLE_ACCEPT_HEADER:
            score = min(95, 60 + round((fill - TABLE_ACCEPT_FILL) * 70))
            note = (f"{len(rows)} rows by {t.col_count} columns, "
                    f"{fill*100:.0f}% of cells filled and a full header row.")
        else:
            score = 45
            note = (f"{len(rows)} rows by {t.col_count} columns, but only "
                    f"{fill*100:.0f}% of cells hold anything. It may be a table, or "
                    "it may be text that happens to line up. Worth a look.")
        x0, y0, x1, y1 = t.bbox
        # A grid whose rows cannot be made to agree on a column count is not
        # describable as a table, so it is not offered as one and its text is
        # tagged normally instead. Emitting it anyway fails PDF/UA 7.2, and the
        # grids that fail this are the ones least likely to be real tables.
        if not _rows_consistent(cells, t.col_count):
            for eid in used:
                taken.discard(eid)
            continue
        out.append(Table(id=f"p{pno}t{ti}", page=pno, rows=len(rows),
                         cols=t.col_count, bbox=(x0, y0, x1, y1), cells=cells,
                         score=score, confidence=_score_to_band(score),
                         accepted=score >= 60, note=note))
        # Nothing ruled? A table can be drawn as one filled rectangle per cell with no
    # lines at all, which the ruled pass sees as a handful of one-row fragments and
    # discards. The columns then come from the painted geometry, which is a fact
    # about the page rather than a guess at where text happens to line up.
    if not out and lines:
        shaped = _shape_table(page, lines, pno, page.rect.width, page.rect.height)
        if shaped:
            t = _table_from_shape(shaped, elements, pno, taken)
            if t:
                out.append(t)
    return out


def _table_from_shape(shaped, elements, pno, taken):
    """Turn a painted-cell grid into a Table, claiming the elements inside it."""
    bands, rows = shaped["bands"], shaped["rows"]
    cells, used = [], []
    for r, row in enumerate(rows):
        by_col = {}
        for _y, c, ln in row:
            by_col.setdefault(c, []).append(ln)
        for c, (bx0, bx1) in enumerate(bands):
            mine = by_col.get(c, [])
            if not mine:
                cells.append(Cell(row=r, col=c, bbox=(bx0, 0, bx1, 0),
                                  header=(r == 0), elements=[], preview=""))
                continue
            y0 = min(l["bbox"][1] for l in mine)
            y1 = max(l["bbox"][3] for l in mine)
            ids, preview = [], []
            for e in elements:
                if e.id in taken or e.kind != "text" or e.role == "Artifact":
                    continue
                ex = (e.bbox[0] + e.bbox[2]) / 2
                ey = (e.bbox[1] + e.bbox[3]) / 2
                if bx0 - 2 <= ex <= bx1 + 2 and y0 - 6 <= ey <= y1 + 6:
                    ids.append(e.id)
                    preview.append(e.text)
            used.extend(ids)
            cells.append(Cell(row=r, col=c, bbox=(bx0, y0, bx1, y1),
                              header=(r == 0), elements=ids,
                              preview=" ".join(preview)[:60]))
    if not used:
        return None
    taken.update(used)
    fill = sum(1 for c in cells if c.elements) / max(1, len(cells))
    return Table(
        id=f"p{pno}st", page=pno, rows=len(rows), cols=len(bands),
        bbox=shaped["bbox"], cells=cells,
        score=min(92, 62 + round((fill - 0.5) * 60)),
        confidence="high" if fill >= 0.8 else "medium", accepted=True,
        note=(f"{len(rows)} rows by {len(bands)} columns. It has no ruling lines, "
              f"but its cells are painted as {len(bands)} repeated columns and "
              f"{fill*100:.0f}% of them hold text."))
