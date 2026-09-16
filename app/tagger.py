"""Write a real PDF structure tree into a document that has none.

This is the part no free tool does.  `harden_pdf.py` repairs a tree that already
exists; this builds one, using the roles a human confirmed in the UI.

Two things happen per page:

1. **Content-stream surgery.** Every mark on the page is wrapped either in a
   marked-content sequence with an MCID (if it carries meaning) or in
   `/Artifact BMC … EMC` (if it is decoration).  PDF/UA 7.1 requires that
   *nothing* is left unmarked.  Rewriting happens through pikepdf's instruction
   tokeniser rather than raw bytes, which sidesteps the classic trap that
   `BDC`/`BMC` are postfix operators.

2. **Structure tree.** `/StructTreeRoot → /Document → …` with a `/ParentTree`
   mapping each MCID back to its structure element, so a screen reader can walk
   the document in the order the user confirmed.
"""
import pikepdf
from pikepdf import Array, Dictionary, Name, Operator, String

from .analyse import walk_content, walk_streams
from .hardening import harden

# Structure types we emit that are not standard PDF 1.7 types need a RoleMap.
_STANDARD = {
    "Document", "P", "H1", "H2", "H3", "H4", "H5", "H6", "L", "LI", "Lbl",
    "LBody", "Figure", "Caption", "Span", "BlockQuote", "Table", "TR", "TH",
    "TD", "Link", "Artifact", "NonStruct", "Sect", "Part",
    # Form is a standard type and must not be remapped. Left out of this set it
    # was mapped to Span, which fails twice over: 7.1 says a standard tag shall
    # not be remapped, and 7.18.4 then sees the widget nested in a Span rather
    # than a Form and rejects it too.
    "Form", "Formula", "TOC", "TOCI", "Note", "Reference", "Index", "Div",
    "Art", "Quote", "Code", "Annot", "THead", "TBody", "TFoot",
}
_LIST_ROLES = {"LI", "LI-ordered"}
# Marked-content operators, stripped out on the way through so tagging is
# repeatable rather than cumulative.
_MARK_OPS = {"BDC", "BMC", "EMC"}


def _bdc(tag, mcid):
    return pikepdf.ContentStreamInstruction(
        [Name("/" + tag), Dictionary(MCID=mcid)], Operator("BDC"))


def _bmc_artifact():
    return pikepdf.ContentStreamInstruction(
        [Name.Artifact], Operator("BMC"))


def _emc():
    return pikepdf.ContentStreamInstruction([], Operator("EMC"))


def _rewrite_streams(pdf, page, elements):
    """Wrap every mark on the page, in whichever stream actually paints it.

    Marked-content ids are scoped to a single content stream, so a form XObject
    gets its own id space and its own `/StructParents`, and a structure element
    pointing into it must use a marked-content reference rather than a bare
    integer. That is the whole reason form content needed more than a wider walk.

    Returns (per_element, maps):
      per_element: {element id: {"mcids": [(stream, id)], "lbl": [(stream, id)]}}
      maps:        {stream: [owning struct elem per id]}  -> becomes ParentTree
    """
    streams, units = walk_streams(page)

    tagged = {}
    for e in elements:
        if e.role == "Artifact":
            continue
        tag = "LI" if e.role in _LIST_ROLES else e.role
        label_ops = set()
        if e.role in _LIST_ROLES and e.marker and len(e.ops) > 1:
            label_ops = {tuple(e.ops[0])}
        for o in e.ops:
            o = tuple(o)
            tagged[o] = (tag, e.id, o in label_ops)

    artifacts = set()
    for u in units:
        o = tuple(u["ops"])
        if o in tagged:
            continue
        if u["kind"] == "form":
            # Never artifact a form that carries text or its own marked content:
            # its inner content is handled in its own stream, and wrapping the
            # invocation would bury tagged content inside an artifact (7.1 t2).
            key = u.get("form")
            if key in streams or u.get("has_text") or u.get("has_marked"):
                continue
        artifacts.add(o)

    counters = {k: 0 for k in streams}
    per_element, maps = {}, {k: [] for k in streams}
    opens = {k: {} for k in streams}
    closes = {k: {} for k in streams}

    for o in sorted(set(tagged) | artifacts):
        key, a, b = o
        if key not in streams:
            continue
        if o in tagged:
            tag, eid, is_label = tagged[o]
            mcid = counters[key]
            counters[key] += 1
            opens[key].setdefault(a, []).append(
                _bdc(tag if not is_label else "Lbl", mcid))
            slot = per_element.setdefault(eid, {"mcids": [], "lbl": []})
            slot["lbl" if is_label else "mcids"].append((key, mcid))
            maps[key].append(None)
        else:
            opens[key].setdefault(a, []).append(_bmc_artifact())
        closes[key][b] = closes[key].get(b, 0) + 1

    for key, info in streams.items():
        if not opens[key] and not closes[key]:
            continue
        out = []
        for i, item in enumerate(info["ins"]):
            for op in opens[key].get(i, []):
                out.append(op)
            if str(item.operator) not in _MARK_OPS:
                out.append(item)
            for _ in range(closes[key].get(i, 0)):
                out.append(_emc())
        data = pikepdf.unparse_content_stream(out)
        if key == "":
            page.Contents = pdf.make_stream(data)
        else:
            info["owner"].write(data)
    return per_element, maps, streams


def _mk(pdf, parent, stype, **kw):
    d = Dictionary(Type=Name.StructElem, S=Name("/" + stype), P=parent, **kw)
    return pdf.make_indirect(d)


def _emit_table(pdf, parent, page, table, mc, attach, kid, used_roles):
    """Emit /Table > /TR > /TH|/TD for a table the user has accepted.

    Spans are what make this valid rather than merely present: PDF/UA requires
    every row to account for the full column count, so a merged cell has to say
    how far it reaches instead of the rows beneath it simply being short.
    """
    used_roles.update({"Table", "TR", "TH", "TD"})
    tbl = _mk(pdf, parent, "Table", K=Array(), Pg=page.obj)
    by_row = {}
    for c in table.cells:
        by_row.setdefault(c.row, []).append(c)
    for r in sorted(by_row):
        tr = _mk(pdf, tbl, "TR", K=Array())
        for c in sorted(by_row[r], key=lambda x: x.col):
            refs = []
            for eid in c.elements:
                slot = mc.get(eid, {"mcids": [], "lbl": []})
                refs.extend(slot["lbl"] + slot["mcids"])
            cell = _mk(pdf, tr, "TH" if c.header else "TD",
                       K=Array([kid(sk, m) for sk, m in refs]), Pg=page.obj)
            attrs = Dictionary(O=Name.Table)
            if c.header:
                attrs[Name.Scope] = Name.Column
            if c.rowspan > 1:
                attrs[Name.RowSpan] = c.rowspan
            if c.colspan > 1:
                attrs[Name.ColSpan] = c.colspan
            if len(attrs) > 1:
                cell.A = pdf.make_indirect(attrs)
            attach(cell, refs)
            tr.K.append(cell)
        tbl.K.append(tr)
    return tbl


def _emit_list(pdf, parent, page, run, mc, attach, used_roles, elem_by_id=None,
               kid=None):
    """Emit one list, rebuilding nesting from the depths the analyser found.

    A sub-list belongs inside its parent item's `/LBody`, so a screen reader
    announces "list of 3" with a nested list under item 1, rather than one flat
    run of six items.
    """
    stack = []          # [{'level': n, 'L': obj, 'last_body': obj|None}]
    top = None
    for item in run:
        lvl = max(1, item.level)
        while stack and stack[-1]["level"] > lvl:
            stack.pop()
        if not stack or stack[-1]["level"] < lvl:
            host = stack[-1]["last_body"] if stack else None
            lst = _mk(pdf, host if host is not None else parent, "L", K=Array())
            lst.A = pdf.make_indirect(Dictionary(
                O=Name.List,
                ListNumbering=Name.Decimal if item.role == "LI-ordered" else Name.Disc))
            used_roles.add("L")
            if host is not None:
                host.K.append(lst)
            elif top is None:
                top = lst
            stack.append({"level": lvl, "L": lst, "last_body": None})
        entry = stack[-1]
        slot = mc.get(item.id, {"mcids": [], "lbl": []})
        li = _mk(pdf, entry["L"], "LI", K=Array())
        used_roles.add("LI")
        if slot["lbl"]:
            lbl = _mk(pdf, li, "Lbl", Pg=page.obj,
                      K=Array([kid(sk, m) for sk, m in slot["lbl"]]))
            attach(lbl, slot["lbl"])
            li.K.append(lbl)
        body = _mk(pdf, li, "LBody", Pg=page.obj,
                   K=Array([kid(sk, m) for sk, m in slot["mcids"]]))
        attach(body, slot["mcids"])
        li.K.append(body)
        entry["L"].K.append(li)
        entry["last_body"] = body
        if elem_by_id is not None:
            elem_by_id[item.id] = (body, item)
    return top


def _clear_previous_tagging(pdf):
    """Forget any structure a previous pass, or another tool, left behind.

    The tree itself is replaced wholesale, but the pointers *into* it live on
    pages and annotations and would otherwise dangle at indices in a ParentTree
    that no longer has them.
    """
    for key in ("/StructTreeRoot", "/MarkInfo"):
        if key in pdf.Root:
            del pdf.Root[key]
    _strip_form_marks(pdf)
    for page in pdf.pages:
        if "/StructParents" in page:
            del page["/StructParents"]
        annots = page.get("/Annots")
        if not annots:
            continue
        for annot in annots:
            if "/StructParent" in annot:
                del annot["/StructParent"]


def _strip_form_marks(pdf, seen=None, res=None, depth=0):
    """Remove marked content from inside form XObjects.

    Stripping only the page stream leaves a previous tool's tagging alive inside
    its forms.  It then sits under whatever this pass writes, which is how tagged
    content ends up inside an artifact.
    """
    if depth > 4:
        return
    seen = seen if seen is not None else set()
    sources = [p.get("/Resources") for p in pdf.pages] if res is None else [res]
    for r in sources:
        if r is None:
            continue
        for _name, xo in (r.get("/XObject", {}) or {}).items():
            if str(xo.get("/Subtype", "")) != "/Form":
                continue
            try:
                key = xo.objgen
                if key in seen:
                    continue
                seen.add(key)
            except Exception:
                pass
            try:
                ins = pikepdf.parse_content_stream(xo)
            except Exception:
                continue
            kept = [i for i in ins if str(i.operator) not in _MARK_OPS]
            if len(kept) != len(ins):
                xo.write(pikepdf.unparse_content_stream(kept))
            _strip_form_marks(pdf, seen, xo.get("/Resources"), depth + 1)


def _tag_annots(pdf, page, pm, document, doc_kids, elem_by_id, nums, next_key,
                used_roles):
    """Give every link annotation a /Link parent and a description.

    Four separate PDF/UA rules land on an untagged link: the page needs
    `/Tabs /S`, the annotation needs `/Contents`, and it must sit under a /Link
    structure element that is reachable from the tree.
    """
    annots = page.get("/Annots")
    if not annots:
        return next_key
    page.Tabs = Name.S

    # Widgets first: a form field has to sit inside a /Form element and carry a
    # /TU tooltip, which is the name a screen reader reads out. Both are separate
    # PDF/UA rules (7.18.4 and 7.18.1), and tagging the page around a field
    # satisfies neither.
    fields = {e.id: e for e in pm.elements if e.kind == "field"}
    used_widgets = set()
    for e in sorted(fields.values(), key=lambda x: x.order):
        idx = e.id.rsplit("f", 1)[-1]
        try:
            annot = annots[int(idx)]
        except (ValueError, IndexError):
            continue
        if str(annot.get("/Subtype", "")) != "/Widget":
            continue
        used_widgets.add(int(idx))
        used_roles.add("Form")
        holder = _mk(pdf, document, "Form", K=Array(), Pg=page.obj)
        holder.K.append(Dictionary(Type=Name.OBJR, Obj=annot, Pg=page.obj))
        doc_kids.append(holder)
        annot.StructParent = next_key
        nums.append(next_key)
        nums.append(holder)
        next_key += 1
        name = (e.alt or "").strip()
        if name:
            annot.TU = String(name)
            # The field dictionary can be the widget itself or its parent.
            parent = annot.get("/Parent")
            if parent is not None and "/T" in parent:
                parent.TU = String(name)

    for i, annot in enumerate(annots):
        if i in used_widgets:
            continue
        if str(annot.get("/Subtype", "")) != "/Link":
            continue
        flags = int(annot.get("/F", 0) or 0)
        if flags & 2:                        # hidden
            continue
        rect = [float(v) for v in annot.get("/Rect", [0, 0, 0, 0])]
        rx0, ry0, rx1, ry1 = min(rect[0], rect[2]), min(rect[1], rect[3]), \
            max(rect[0], rect[2]), max(rect[1], rect[3])

        # Match the annotation to the text under it; pymupdf boxes are top-down.
        best, best_overlap = None, 0.0
        for obj, e in elem_by_id.values():
            if e.kind != "text":
                continue
            ex0, ey0, ex1, ey1 = e.bbox
            ay0, ay1 = pm.height - ey1, pm.height - ey0
            ox = max(0, min(rx1, ex1) - max(rx0, ex0))
            oy = max(0, min(ry1, ay1) - max(ry0, ay0))
            if ox * oy > best_overlap:
                best, best_overlap = (obj, e), ox * oy

        host = best[0] if best else document
        text = (best[1].text if best else "").strip()
        uri = ""
        action = annot.get("/A")
        if action is not None and "/URI" in action:
            uri = str(action["/URI"])
        desc = text or uri or "Link"
        if uri and text and uri not in text:
            desc = f"{text} ({uri})"

        link = _mk(pdf, host, "Link", Pg=page.obj)
        used_roles.add("Link")
        objr = pdf.make_indirect(Dictionary(Type=Name.OBJR, Obj=annot,
                                            Pg=page.obj))
        link.K = Array([objr])
        if isinstance(host.get("/K"), Array):
            host.K.append(link)
        elif host is document:
            doc_kids.append(link)
        else:
            host.K = Array([host.K, link]) if "/K" in host else Array([link])

        annot.Contents = String(desc)
        annot.StructParent = next_key
        nums.append(next_key)
        nums.append(link)
        next_key += 1
    return next_key


def build(model, out_path, progress=None):
    """Apply `model` (roles confirmed by the user) and write a tagged PDF."""
    pdf = pikepdf.open(model.path)
    _clear_previous_tagging(pdf)
    struct_root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    document = _mk(pdf, struct_root, "Document")
    doc_kids = Array()
    nums = Array()
    used_roles = set()
    next_key = 0

    for pm in model.pages:
        page = pdf.pages[pm.number]
        live = [e for e in pm.elements if e.role != "Artifact"]
        live.sort(key=lambda e: e.order)
        mc, maps, streams = _rewrite_streams(pdf, page, pm.elements)
        elem_by_id = {}

        # One ParentTree key per content stream: the page, plus any form XObject
        # that ended up carrying marked content.
        stream_keys = {}
        for skey, arr in maps.items():
            if not arr:
                continue
            stream_keys[skey] = next_key
            owner = streams[skey]["owner"]
            if skey == "":
                page.StructParents = next_key
            else:
                owner["/StructParents"] = next_key
            next_key += 1
        page_key = stream_keys.get("")

        def attach(elem_obj, refs):
            for skey, mcid in refs:
                maps[skey][mcid] = elem_obj

        def kid(skey, mcid):
            """A bare id for the page's own stream, an MCR for anything else."""
            if skey == "":
                return mcid
            return pdf.make_indirect(Dictionary(
                Type=Name.MCR, Pg=page.obj,
                Stm=streams[skey]["owner"], MCID=mcid))

        # Accepted tables own their cells' elements: those must not also be emitted
        # as loose paragraphs, or the text is tagged twice.
        tables = [t for t in getattr(pm, "tables", []) if t.accepted]
        claimed_by_table = {eid for t in tables for c in t.cells for eid in c.elements}
        table_at = {}
        for t in tables:
            first = min((e.order for e in live if e.id in
                         {i2 for c in t.cells for i2 in c.elements}), default=None)
            if first is not None:
                table_at.setdefault(first, []).append(t)

        i = 0
        while i < len(live):
            e = live[i]
            for t in table_at.get(e.order, []):
                doc_kids.append(_emit_table(pdf, document, page, t, mc, attach,
                                            kid, used_roles))
            if e.id in claimed_by_table:
                i += 1
                continue
            if e.role in _LIST_ROLES:
                run = []
                while i < len(live) and live[i].role in _LIST_ROLES:
                    run.append(live[i])
                    i += 1
                top = _emit_list(pdf, document, page, run, mc, attach,
                                 used_roles, elem_by_id, kid)
                if top is not None:
                    doc_kids.append(top)
                continue

            slot = mc.get(e.id, {"mcids": [], "lbl": []})
            all_mcids = slot["lbl"] + slot["mcids"]
            if not all_mcids:
                i += 1
                continue
            stype = e.role
            kw = {"Pg": page.obj,
                  "K": Array([kid(sk, m) for sk, m in all_mcids])}
            if stype == "Figure":
                kw["Alt"] = String(e.alt or "")
                if e.bbox and any(e.bbox):
                    x0, y0, x1, y1 = e.bbox
                    kw["A"] = pdf.make_indirect(Dictionary(
                        O=Name.Layout, BBox=Array([x0, y0, x1, y1])))
            obj = _mk(pdf, document, stype, **kw)
            elem_by_id[e.id] = (obj, e)
            used_roles.add(stype)
            attach(obj, all_mcids)
            doc_kids.append(obj)
            i += 1

        for skey, key_num in sorted(stream_keys.items(), key=lambda kv: kv[1]):
            arr = maps[skey]
            for idx, v in enumerate(arr):
                if v is None:
                    arr[idx] = document
            nums.append(key_num)
            nums.append(pdf.make_indirect(Array(arr)))
        next_key = _tag_annots(pdf, page, pm, document, doc_kids,
                               elem_by_id, nums, next_key, used_roles)
        if progress:
            progress(pm.number + 1, len(model.pages))

    document.K = doc_kids
    struct_root.K = Array([document])
    struct_root.ParentTree = pdf.make_indirect(Dictionary(Nums=nums))
    struct_root.ParentTreeNextKey = next_key
    rolemap = Dictionary()
    for r in used_roles:
        if r not in _STANDARD:
            rolemap[Name("/" + r)] = Name.Span
    struct_root.RoleMap = rolemap
    pdf.Root.StructTreeRoot = struct_root
    harden(pdf, model.title, model.lang)
    pdf.save(out_path)
    return out_path
