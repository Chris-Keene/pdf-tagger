"""Document-level requirements that a structure tree alone does not satisfy.

Four things sit outside the tree and fail PDF/UA on their own:

* `/Lang` on the catalog — without it every text run fails 7.2 t34 individually,
  which is why one missing key produces dozens of failed checks.
* `/MarkInfo << /Marked true >>` — the flag that says "this file is tagged".
* `/ViewerPreferences << /DisplayDocTitle true >>` — show the title, not the
  filename, in the window chrome.
* An XMP metadata stream declaring `pdfuaid:part` **and** `dc:title`.  Adding the
  stream without the title just swaps one failure for another.
"""
import pikepdf
from pikepdf import Dictionary, Name, String

XMP = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:pdfuaid="http://www.aiim.org/pdfua/ns/id/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:pdf="http://ns.adobe.com/pdf/1.3/"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/">
   <pdfuaid:part>1</pdfuaid:part>
   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">{title}</rdf:li></rdf:Alt></dc:title>
   <dc:language><rdf:Bag><rdf:li>{lang}</rdf:li></rdf:Bag></dc:language>
   <xmp:CreatorTool>Trellis</xmp:CreatorTool>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""


def _esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def harden(pdf, title, lang="en-GB"):
    """Apply the four document-level requirements in place."""
    # Read DocInfo *before* touching XMP: pikepdf's metadata API can rewrite it.
    existing = ""
    try:
        if pdf.trailer.get("/Info") and "/Title" in pdf.docinfo:
            existing = str(pdf.docinfo["/Title"]).strip()
    except Exception:
        pass
    title = (title or existing or "Untitled document").strip()

    pdf.Root.Lang = String(lang)
    pdf.Root.MarkInfo = Dictionary(Marked=True)
    vp = pdf.Root.get("/ViewerPreferences")
    if vp is None:
        pdf.Root.ViewerPreferences = Dictionary(DisplayDocTitle=True)
    else:
        vp[Name.DisplayDocTitle] = True

    with pdf.open_metadata(set_pikepdf_as_editor=False,
                           update_docinfo=False) as _:
        pass  # ensure a metadata stream slot exists before we overwrite it
    xmp = XMP.format(title=_esc(title), lang=_esc(lang)).encode("utf-8")
    pdf.Root.Metadata = pdf.make_stream(xmp)
    pdf.Root.Metadata[Name.Type] = Name.Metadata
    pdf.Root.Metadata[Name.Subtype] = Name.XML

    if pdf.trailer.get("/Info") is None:
        pdf.trailer.Info = pdf.make_indirect(Dictionary())
    pdf.trailer.Info[Name.Title] = String(title)
    return title
