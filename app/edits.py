"""Edits that rework which content belongs to which item.

Kept out of the browser deliberately. Changing a role or a reading position only
moves numbers around, but splitting an item redistributes *operator ranges*, and
getting that wrong writes a structure tree that points at the wrong bytes — a
file that looks tagged and reads as nonsense. One implementation, in the language
the tagger is written in, so the tests exercise the code that actually ships.
"""

import re


def _bbox_of(parts):
    return (min(p["bbox"][0] for p in parts), min(p["bbox"][1] for p in parts),
            max(p["bbox"][2] for p in parts), max(p["bbox"][3] for p in parts))


def split_groups(parts):
    """Group an item's lines into the pieces a split should produce.

    Clustered by horizontal extent, because that is what tells columns from
    wrapped text: the lines of a wrapped paragraph start at the same place and
    overlap one another, while two cells side by side do not overlap at all.

    When everything overlaps there are no columns to find, and an explicit
    request to split falls back to one piece per line rather than doing nothing.
    """
    groups = []
    for pt in sorted(parts, key=lambda p: p["bbox"][0]):
        hit = next((g for g in groups if pt["bbox"][0] <= g["x1"] + 6), None)
        if hit:
            hit["items"].append(pt)
            hit["x1"] = max(hit["x1"], pt["bbox"][2])
        else:
            groups.append({"items": [pt], "x1": pt["bbox"][2]})
    if len(groups) > 1:
        out = [sorted(g["items"], key=lambda p: p["bbox"][1]) for g in groups]
    else:
        out = [[p] for p in sorted(parts, key=lambda p: (p["bbox"][1], p["bbox"][0]))]
    # Down the page first, then across it, so a row of cells and a stack of
    # lines both come out in the order they would be read.
    out.sort(key=lambda g: (round(min(p["bbox"][1] for p in g) / 4),
                            min(p["bbox"][0] for p in g)))
    return out


def _cell_for(pm, bbox):
    """The table cell a rectangle sits in, by its centre point."""
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    for t in pm.tables:
        for c in t.cells:
            x0, y0, x1, y1 = c.bbox
            if x0 - 2 <= cx <= x1 + 2 and y0 - 2 <= cy <= y1 + 2:
                return c
    return None


def _retarget_tables(pm, old_id, pieces):
    """Keep table cells pointing at elements that still exist.

    Cells hold element *ids*, so an id that has just been split or joined away
    leaves a cell referencing nothing: the cell comes out empty and the table is
    hollow, while the text is tagged loose somewhere else. Nothing flags it, and
    the file still validates.

    Each piece is placed in the cell that geometrically contains it, so splitting
    a header row that was wrongly merged fixes the table as well: the two halves
    land in the two cells they were always drawn in. A piece belonging to no cell
    is simply left out of the table, which is right, because it is not in it.
    """
    for t in pm.tables:
        for c in t.cells:
            if old_id in c.elements:
                c.elements = [x for x in c.elements if x != old_id]
    for e in pieces:
        c = _cell_for(pm, e.bbox)
        if c is not None and e.id not in c.elements:
            c.elements.append(e.id)


def can_split(e):
    return e is not None and e.kind == "text" and len(e.parts or []) > 1


def split(model, eid):
    """Break one item into the separate pieces its geometry implies."""
    from .model import Element
    for pm in model.pages:
        e = next((x for x in pm.elements if x.id == eid), None)
        if e is None:
            continue
        if not can_split(e):
            return False, "That item is a single piece, so there is nothing to split."
        groups = split_groups(e.parts)
        if len(groups) < 2:
            return False, "That item is a single piece, so there is nothing to split."
        made = []
        for i, g in enumerate(groups):
            made.append(Element(
                id=f"{e.id}s{i}", page=e.page, kind="text",
                role=e.role, auto_role=e.auto_role,
                text=re.sub(r"\s+", " ", " ".join(p["text"] for p in g)).strip(),
                bbox=_bbox_of(g), size=e.size, font=e.font, bold=e.bold,
                order=e.order + i, column=e.column,
                marker=e.marker if i == 0 else "", indent=e.indent, level=e.level,
                ops=[tuple(o) for p in g for o in p["ops"]],
                parts=[dict(p) for p in g],
                score=100, confidence="high", reviewed=True,
                note="You split this out of a larger item."))
        at = pm.elements.index(e)
        for x in pm.elements:
            if x.order > e.order:
                x.order += len(made) - 1
        pm.elements[at:at + 1] = made
        _retarget_tables(pm, e.id, made)
        renumber(pm)
        return True, made[0].id
    return False, "Item not found."


def join_up(model, eid):
    """Fold an item onto the one before it in the reading order."""
    for pm in model.pages:
        e = next((x for x in pm.elements if x.id == eid), None)
        if e is None:
            continue
        live = sorted([x for x in pm.elements if x.role != "Artifact"],
                      key=lambda x: x.order)
        i = next((n for n, x in enumerate(live) if x.id == eid), -1)
        if i < 1:
            return False, "There is nothing before this to join it onto."
        prev = live[i - 1]
        # Text and images never merge. The result is incoherent either way: a
        # Figure whose text is a sentence, or a paragraph that owns an image's
        # drawing operators. Reading order routinely puts a picture between two
        # halves of a split sentence, so this is easy to hit by accident.
        if prev.kind != e.kind:
            what = "an image" if prev.kind == "image" else "text"
            return False, (f"The item before this one is {what}, and text and "
                           "images cannot be joined together. Move this item so "
                           "it sits directly after the one you want it joined to.")
        prev.text = re.sub(r"\s+", " ",
                           f'{prev.text or ""} {e.text or ""}').strip()
        prev.bbox = (min(prev.bbox[0], e.bbox[0]), min(prev.bbox[1], e.bbox[1]),
                     max(prev.bbox[2], e.bbox[2]), max(prev.bbox[3], e.bbox[3]))
        prev.ops = list(prev.ops) + list(e.ops)
        prev.parts = list(prev.parts or []) + list(e.parts or [])
        prev.reviewed, prev.score, prev.confidence = True, 100, "high"
        prev.note = "You joined this together from separate items."
        # Joining can turn a short heading into a passage. A quotation split into
        # two halves reads as two headings until the halves are put back, and
        # leaving the role alone would keep a whole paragraph in the heading list.
        from .analyse import _is_prose, _is_quotation
        # Putting the halves back can reveal a quotation: the opening mark is on
        # one half and the closing mark on the other, so neither looks like one
        # on its own.
        if _is_quotation(prev.text):
            prev.auto_role = prev.role = "BlockQuote"
            prev.note = ("You joined this together, and it opens and closes with "
                         "quote marks, so it reads as a quotation.")
        elif prev.role.startswith("H") and _is_prose(prev.text):
            prev.auto_role = prev.role = "P"
            prev.note = ("You joined this together, and at "
                         f"{len(prev.text.split())} words it now reads as a "
                         "passage rather than a heading.")
        pm.elements.remove(e)
        _retarget_tables(pm, e.id, [prev])
        renumber(pm)
        return True, prev.id
    return False, "Item not found."


def renumber(pm):
    live = sorted([x for x in pm.elements if x.role != "Artifact"],
                  key=lambda x: x.order)
    for i, x in enumerate(live):
        x.order = i
    for x in pm.elements:
        if x.role == "Artifact":
            x.order = 10000
