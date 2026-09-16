"""What still needs a human, expressed in plain English.

Conformance checkers answer a narrow question: does a key exist.  They cannot
tell you whether the alt text means anything, whether the reading order makes
sense, or whether a heading level was skipped.  Those are the things that
actually decide whether a screen reader user can use the document, so they are
what this app asks the user about.
"""
import os
import re

from .analyse import heading_warnings
from .model import REVIEW_THRESHOLD

# Alt text that passes every conformance checker and helps nobody.
_FILENAME = re.compile(r"^[\w \-]+\.(png|jpe?g|gif|svg|webp|bmp|tiff?|pdf)$", re.I)
_PATHLIKE = re.compile(r"(^/|^[A-Za-z]:\\|/var/folders/|/tmp/|/Users/|\\Users\\)")
_JUNK = {"image", "picture", "photo", "graphic", "img", "logo", "icon",
         "screenshot", "chart", "diagram", "untitled", "placeholder", "figure"}
_PREFIX = re.compile(r"^\s*(image|picture|photo|graphic|screenshot)\s+(of|showing)\b", re.I)

SEVERITY = ("blocker", "warning", "suggestion")


def _issue(sev, kind, message, fix="", page=None, eid=None):
    return {"severity": sev, "kind": kind, "message": message, "fix": fix,
            "page": page, "id": eid}


def alt_text_issues(text, alt):
    """Judge one alt string. Returns a list of (severity, message, fix)."""
    a = (alt or "").strip()
    out = []
    if not a:
        return [("blocker", "No description yet.",
                 "Describe what the image shows, or mark it decorative.")]
    if _PATHLIKE.search(a):
        out.append(("blocker",
                    "This looks like a file path, which also leaks where the file came from.",
                    "Replace it with a description of what the image shows."))
    elif _FILENAME.match(a):
        out.append(("blocker", "This is a filename, not a description.",
                    "Say what the image shows instead."))
    elif a.lower().strip(" .") in _JUNK:
        out.append(("warning", f"“{a}” does not say anything a reader can use.",
                    "Say what it shows, or mark it decorative if it carries no meaning."))
    if _PREFIX.match(a):
        out.append(("suggestion",
                    "Starts with “image of” — a screen reader already says it is an image.",
                    "Drop the prefix and start with the content."))
    if a and len(a) < 4:
        out.append(("warning", "Very short description.",
                    "A few words that say what it shows work better."))
    if len(a) > 500:
        out.append(("suggestion", "Very long description.",
                    "Keep alt text to a sentence or two; put detail in the page text."))
    return out


def tables_to_check(model):
    """Tables the app is not sure about, in reading order."""
    out = []
    for p in model.pages:
        for t in getattr(p, "tables", []):
            if not t.reviewed and t.score < REVIEW_THRESHOLD:
                out.append(t)
    return out


def needs_review(e):
    """Is this a proposal the user should look at before it ships?

    Images are excluded on purpose: they have their own step, where the real
    question is the description rather than the role, and counting them twice
    would make both lists less useful.
    """
    return (e.kind == "text" and e.role != "Artifact"
            and not e.reviewed and e.score < REVIEW_THRESHOLD
            and bool(e.text.strip()))


def review(model):
    """Everything worth telling the user about, ordered by how much it matters."""
    issues = []

    if not (model.title or "").strip():
        issues.append(_issue("blocker", "title",
                             "The document has no title.",
                             "A title is required, and it is what a screen reader "
                             "announces when the file opens."))
    else:
        t = model.title.strip()
        if _FILENAME.match(t) or t.lower().endswith(".pdf"):
            issues.append(_issue("warning", "title",
                                 f"The title “{t}” is a filename.",
                                 "Use the document's real name."))
    if not (model.lang or "").strip():
        issues.append(_issue("blocker", "lang", "No language is set.",
                             "The language tells a screen reader which voice to use."))

    if getattr(model, "was_encrypted", False):
        issues.append(_issue(
            "warning", "encryption",
            "This PDF has a password or usage restrictions on it, and the tagged "
            "copy will not.",
            "Tagging has to rewrite the file, and it is written out unprotected. "
            "The content is identical, but if the restrictions matter, re-apply them "
            "to the copy before you send it on."))

    # A file can be tagged and still say nothing useful. Worth its own message,
    # because "already tagged" otherwise reads as "already fine" and the user
    # never looks at the one thing that was actually missing.
    if getattr(model, "adopted_flat", False):
        heads = sum(1 for p in model.pages for e in p.elements
                    if e.role.startswith("H"))
        if getattr(model, "adopted_reason", "") == "wrong":
            why = getattr(model, "adopted_why", "")
            issues.append(_issue(
                "warning", "adopted-flat",
                "This PDF is already tagged, but the tags do not match what is on "
                f"the page, so they have been set aside and {heads} headings "
                "worked out afresh.",
                (f"The giveaway: {why}. " if why else "") +
                "A tree can be full of headings and lists and still be wrong, and "
                "that is harder to spot than having none at all, because the file "
                "looks like somebody thought about it. The tables and images have "
                "been kept, since those were sound. Everything on the structure "
                "step is a fresh proposal, so it is worth a read through."))
        else:
            issues.append(_issue(
                "warning", "adopted-flat",
                "This PDF is already tagged, but its structure has no headings at "
                f"all - every paragraph is just a paragraph. {heads} headings have "
                "been proposed instead.",
                "Some producers, Google Docs among them, write a structure tree "
                "without ever marking a heading. Nothing is wrong with the file's "
                "tables or images and those have been kept, but heading navigation "
                "is the main thing a screen reader user relies on, so the headings "
                "here are worth a look on the structure step."))

    adopted = getattr(model, "adopted", 0)
    if adopted and not getattr(model, "adopted_ours", False) and not getattr(model, "adopted_flat", False):
        issues.append(_issue(
            "suggestion", "adopted",
            f"This PDF already carried tags from another tool, and {adopted} of them "
            "have been kept rather than guessed at again.",
            "That is usually the right call, since whoever made the file knew more "
            "about it than a guess from font sizes can. It does mean nobody has "
            "checked them, so if a heading or a list looks wrong on the structure "
            "step, change it."))
    elif adopted and getattr(model, "adopted_ours", False):
        issues.append(_issue(
            "suggestion", "adopted",
            f"{adopted} tag{'s' if adopted != 1 else ''} "
            f"{'were' if adopted != 1 else 'was'} read back from your earlier pass "
            "on this file, so your decisions have been kept.",
            "Anything you confirmed last time is still marked as checked."))

    nested = getattr(model, "nested_text_pages", 0)
    unmappable = getattr(model, "unmappable", 0)
    if nested:
        issues.append(_issue(
            "blocker", "nested",
            f"On {nested} page{'s' if nested > 1 else ''}, the text sits inside a "
            f"nested object that this app cannot tag"
            + (f" ({unmappable} items affected)." if unmappable else "."),
            "This is a limitation, not something you can fix here. PDFs written by "
            "some tools wrap their content in a nested object, and the tagger works "
            "at the page level. Anything affected is left untagged rather than "
            "quietly marked as decoration, so the conformance check will fail and "
            "tell you the truth. Re-export the document from its source, or use a "
            "route that produces a tagged PDF directly."))
    elif unmappable:
        issues.append(_issue(
            "warning", "nested",
            f"{unmappable} piece{'s' if unmappable > 1 else ''} of text could not be "
            "matched to anything on the page, so "
            f"{'they' if unmappable > 1 else 'it'} will not be tagged.",
            "This usually means the PDF stores that text in an unusual way. Check "
            "the exported file reads correctly before sending it."))

    t3 = list(getattr(model, "type3_fonts", []) or [])
    if t3:
        issues.append(_issue(
            "warning", "type3",
            f"This PDF uses {len(t3)} Type 3 font{'s' if len(t3) > 1 else ''}, "
            "which is how some exports store text when a variable font was used.",
            "Worth checking the text comes out as words rather than fragments. "
            "Where this goes wrong a screen reader reads “Mo ile” for “Mobile”, and "
            "no conformance rule catches it. The check on the export step looks for "
            "it. If it is broken, it has to be fixed in the source, not here."))

    missing = list(getattr(model, "unembedded_fonts", []) or [])
    if missing:
        names = ", ".join(missing[:3]) + (f" and {len(missing)-3} more" if len(missing) > 3 else "")
        issues.append(_issue(
            "blocker", "fonts",
            f"The font{'s' if len(missing) > 1 else ''} {names} "
            f"{'are' if len(missing) > 1 else 'is'} not stored inside this PDF.",
            "Tagging cannot fix this one. The glyphs a reader needs are missing from "
            "the file, so it will fail a conformance check and may look different on "
            "another machine. It has to be fixed where the document was made, by "
            "embedding the fonts on export."))

    for w in heading_warnings(model):
        issues.append(_issue("warning", "headings", w["message"],
                             "Heading levels should step down one at a time so the "
                             "document outline makes sense.",
                             page=w["page"], eid=w["id"]))

    if not any(e.role.startswith("H") for p in model.pages for e in p.elements):
        issues.append(_issue("warning", "headings",
                             "Nothing is tagged as a heading.",
                             "Headings are how people skim a document with a screen "
                             "reader. Most decks have at least a title per slide."))

    for p in model.pages:
        for e in p.elements:
            if e.role == "Figure":
                for sev, msg, fix in alt_text_issues(e.text, e.alt):
                    issues.append(_issue(sev, "alt", msg, fix, page=p.number, eid=e.id))
            # Hiding real text as decoration silently removes it from the document.
            if e.role == "Artifact" and e.kind == "text" and len(e.text.strip()) > 25:
                issues.append(_issue(
                    "warning", "hidden",
                    f"“{e.text.strip()[:40]}…” is set to be ignored, but it is real text.",
                    "Anything marked ignored is invisible to a screen reader. "
                    "Give it a role unless it is genuinely decorative.",
                    page=p.number, eid=e.id))
            if needs_review(e):
                issues.append(_issue(
                    "suggestion", "uncertain",
                    f"Worth checking: “{e.text[:44]}” was tagged as "
                    f"{e.role.replace('LI-ordered', 'a numbered item').replace('LI', 'a list item')}.",
                    e.note, page=p.number, eid=e.id))

    for p in model.pages:
        for t in getattr(p, "tables", []):
            if t.reviewed or t.score >= REVIEW_THRESHOLD:
                continue
            issues.append(_issue(
                "suggestion", "table",
                f"Is the {t.rows} by {t.cols} block on page {p.number+1} a table?",
                t.note + (" Accepted as a table it will be announced as one, with "
                          "its header row read out for each cell. Rejected, its text "
                          "is tagged as ordinary paragraphs."),
                page=p.number, eid=t.id))

    unnamed = [e for p in model.pages for e in p.elements
               if e.kind == "field" and not (e.alt or "").strip()]
    if unnamed:
        issues.append(_issue(
            "blocker", "field-name",
            f"{len(unnamed)} form field{'s' if len(unnamed) != 1 else ''} "
            f"{'have' if len(unnamed) != 1 else 'has'} no name.",
            "A screen reader announces a field by its name and nothing else, so an "
            "unnamed one reads as 'edit text' with no clue what to type. Name them "
            "on the images and fields step. Where a label is printed beside the "
            "field the name has already been proposed."))

    # Duplicate alt text across different images usually means copy-paste.
    seen = {}
    for p in model.pages:
        for e in p.elements:
            if e.role == "Figure" and e.alt.strip():
                seen.setdefault(e.alt.strip().lower(), []).append((p.number, e.id))
    for alt, where in seen.items():
        if len(where) > 2:
            issues.append(_issue("suggestion", "alt",
                                 f"{len(where)} images share the description “{alt[:40]}”.",
                                 "If they show different things, describe them "
                                 "separately. If they are decoration, mark them decorative.",
                                 page=where[0][0], eid=where[0][1]))

    order = {s: i for i, s in enumerate(SEVERITY)}
    issues.sort(key=lambda i: (order.get(i["severity"], 9), i["page"] if i["page"] is not None else -1))
    return issues


def to_check(model):
    """Every element still awaiting a look, in reading order across the document."""
    out = []
    for p in model.pages:
        for e in sorted(p.elements, key=lambda x: x.order):
            if needs_review(e):
                out.append(e)
    return out


def summary(model):
    """Counts for the progress display."""
    figs = [e for p in model.pages for e in p.elements if e.role == "Figure"]
    text = [e for p in model.pages for e in p.elements
            if e.kind == "text" and e.role != "Artifact" and e.text.strip()]
    iss = review(model)
    pending = to_check(model)
    confident = [e for e in text if e.score >= REVIEW_THRESHOLD]
    fields = [e for p in model.pages for e in p.elements if e.kind == "field"]
    tables = [t for p in model.pages for t in getattr(p, "tables", [])]
    return {
        "fields": len(fields),
        "fields_todo": sum(1 for e in fields if not (e.alt or "").strip()),
        "tables": len(tables),
        "tables_accepted": sum(1 for t in tables if t.accepted),
        "tables_to_check": len(tables_to_check(model)),
        "text_items": len(text),
        "confident": len(confident),
        "to_check": len(pending),
        "reviewed": sum(1 for e in text if e.reviewed),
        "mean_score": round(sum(e.score for e in text) / len(text)) if text else 100,
        "pages": len(model.pages),
        "tagged": sum(1 for p in model.pages for e in p.elements if e.role != "Artifact"),
        "artifacts": sum(1 for p in model.pages for e in p.elements if e.role == "Artifact"),
        "headings": sum(1 for p in model.pages for e in p.elements if e.role.startswith("H")),
        "figures": len(figs),
        "figures_described": sum(1 for e in figs if e.alt.strip()),
        "blockers": sum(1 for i in iss if i["severity"] == "blocker"),
        "warnings": sum(1 for i in iss if i["severity"] == "warning"),
        "suggestions": sum(1 for i in iss if i["severity"] == "suggestion"),
    }
