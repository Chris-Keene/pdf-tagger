"""Shared data model for the tagging pipeline.

An `Element` is one candidate structure element on one page: a run of text or an
image, together with the content-stream operator ranges that paint it.  The
analyser proposes a role; the user confirms or changes it; the tagger writes it.
"""
from dataclasses import dataclass, field, asdict
from typing import List, Tuple, Optional

# Roles the user can pick, grouped for the UI.  Values are PDF structure types.
ROLE_GROUPS = [
    ("Headings", ["H1", "H2", "H3", "H4", "H5", "H6"]),
    ("Text",     ["P", "Span", "BlockQuote", "Caption"]),
    ("Lists",    ["LI", "LI-ordered"]),
    ("Figures",  ["Figure"]),
    ("Ignore",   ["Artifact"]),
]
ALL_ROLES = [r for _, rs in ROLE_GROUPS for r in rs]

# Roles that require alt text to be PDF/UA conformant.
NEEDS_ALT = {"Figure"}

# Below this confidence the app asks the user to look at a proposal rather than
# assuming it. One constant so the checks and the UI can never disagree.
REVIEW_THRESHOLD = 70


@dataclass
class Element:
    id: str
    page: int
    kind: str                       # 'text' | 'image'
    role: str                       # proposed or confirmed role
    text: str = ""
    alt: str = ""                   # /Alt for figures
    bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)
    size: float = 0.0
    font: str = ""
    bold: bool = False
    order: int = 0                  # reading order within the page
    column: int = 0
    marker: str = ""                # list bullet/number stripped from the text
    indent: float = 0.0             # left edge of the marker, for list nesting
    level: int = 1                  # list nesting depth, 1 = outermost
    ops: List[Tuple[int, int]] = field(default_factory=list)
    auto_role: str = ""             # what the analyser proposed, for "changed?" UI
    score: int = 100                # 0-100 confidence in the proposed role
    confidence: str = "high"        # high | medium | low, derived from score
    # Each line that went into this item, with its own geometry and operator
    # ranges. Kept so the item can be split apart later: an operator range on its
    # own carries no position, so without this a wrongly merged item is welded shut.
    parts: List[dict] = field(default_factory=list)
    # The page's own title, found by position. Anchors heading levels so the same
    # kind of thing gets the same level wherever it appears in the file.
    is_title: bool = False
    # Written by Claude rather than by a person. Kept separate from `reviewed` so
    # a draft can never be mistaken for a decision.
    drafted: bool = False
    reviewed: bool = False          # the user has looked at this and moved on
    note: str = ""                  # why the analyser proposed this

    def to_json(self):
        d = asdict(self)
        d["bbox"] = list(self.bbox)
        d["ops"] = [list(o) for o in self.ops]
        return d

    @staticmethod
    def from_json(d):
        d = dict(d)
        d["bbox"] = tuple(d.get("bbox", (0, 0, 0, 0)))
        d["ops"] = [tuple(o) for o in d.get("ops", [])]
        return Element(**d)


@dataclass
class Cell:
    """One cell, pointing at the elements that already hold its text.

    Deliberately not a copy of the text: keeping the elements as the single source
    means rejecting a table is a flag, not a data migration, and the cells stay in
    step with any role or ordering change the user makes.
    """
    row: int
    col: int
    # The cell's own rectangle. Needed to work out which cell a piece belongs to
    # after an item is split, so a wrongly merged header row ends up spread across
    # the right cells rather than all in the one the merged item happened to sit in.
    bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)
    rowspan: int = 1
    colspan: int = 1
    header: bool = False
    elements: List[str] = field(default_factory=list)
    preview: str = ""

    def to_json(self):
        d = asdict(self)
        d["bbox"] = list(self.bbox)
        return d

    @staticmethod
    def from_json(d):
        d = dict(d)
        d["bbox"] = tuple(d.get("bbox", (0, 0, 0, 0)))
        return Cell(**d)


@dataclass
class Table:
    id: str
    page: int
    rows: int = 0
    cols: int = 0
    bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)
    cells: List[Cell] = field(default_factory=list)
    order: int = 0
    score: int = 0
    confidence: str = "medium"
    reviewed: bool = False
    accepted: bool = True        # rejected tables tag as ordinary text instead
    note: str = ""

    def to_json(self):
        d = asdict(self)
        d["bbox"] = list(self.bbox)
        d["cells"] = [c.to_json() for c in self.cells]
        return d

    @staticmethod
    def from_json(d):
        d = dict(d)
        d["bbox"] = tuple(d.get("bbox", (0, 0, 0, 0)))
        d["cells"] = [Cell.from_json(c) for c in d.get("cells", [])]
        return Table(**d)


@dataclass
class PageModel:
    number: int                     # 0-based
    width: float
    height: float
    elements: List[Element] = field(default_factory=list)
    tables: List[Table] = field(default_factory=list)

    def to_json(self):
        return {"number": self.number, "width": self.width,
                "height": self.height,
                "elements": [e.to_json() for e in self.elements],
                "tables": [t.to_json() for t in self.tables]}


@dataclass
class DocModel:
    path: str
    title: str = ""
    lang: str = "en-GB"
    pages: List[PageModel] = field(default_factory=list)
    was_tagged: bool = False
    # Another tool tagged this file but emitted no headings and no lists, so its
    # text roles were ignored in favour of our own proposals.
    adopted_flat: bool = False
    # Why the file's own text roles were set aside: "flat" (it emitted none) or
    # "wrong" (it emitted some, and they do not line up with the page).
    adopted_reason: str = ""
    adopted_why: str = ""
    unembedded_fonts: List[str] = field(default_factory=list)
    type3_fonts: List[str] = field(default_factory=list)
    unmappable: int = 0             # text elements with no operators to wrap
    nested_text_pages: int = 0      # pages whose text sits inside a form XObject
    adopted: int = 0                # roles taken from tags already in the file
    adopted_ours: bool = False       # ...and this app is what wrote them
    was_encrypted: bool = False      # the source had encryption; the copy will not

    def to_json(self):
        return {"path": self.path, "title": self.title, "lang": self.lang,
                "was_tagged": self.was_tagged,
                "adopted_flat": self.adopted_flat,
                "adopted_reason": self.adopted_reason,
                "adopted_why": self.adopted_why,
                "unembedded_fonts": list(self.unembedded_fonts),
                "type3_fonts": list(self.type3_fonts),
                "unmappable": self.unmappable,
                "nested_text_pages": self.nested_text_pages,
                "adopted": self.adopted,
                "adopted_ours": self.adopted_ours,
                "was_encrypted": self.was_encrypted,
                "pages": [p.to_json() for p in self.pages]}

    @staticmethod
    def from_json(d):
        m = DocModel(path=d["path"], title=d.get("title", ""),
                     lang=d.get("lang", "en-GB"),
                     was_tagged=d.get("was_tagged", False),
                     adopted_flat=d.get("adopted_flat", False),
                     adopted_reason=d.get("adopted_reason", ""),
                     adopted_why=d.get("adopted_why", ""),
                     unembedded_fonts=list(d.get("unembedded_fonts", [])),
                     type3_fonts=list(d.get("type3_fonts", [])),
                     unmappable=d.get("unmappable", 0),
                     nested_text_pages=d.get("nested_text_pages", 0),
                     adopted=d.get("adopted", 0),
                     adopted_ours=d.get("adopted_ours", False),
                     was_encrypted=d.get("was_encrypted", False))
        for p in d.get("pages", []):
            pm = PageModel(number=p["number"], width=p["width"], height=p["height"])
            pm.elements = [Element.from_json(e) for e in p["elements"]]
            pm.tables = [Table.from_json(t) for t in p.get("tables", [])]
            m.pages.append(pm)
        return m
