# Guide sources

The two PDFs in `dist/` are built from the HTML in this folder:

| Source | Output |
|---|---|
| `installing-trellis.html` | `dist/Installing-Trellis.pdf` |
| `how-trellis-works.html` | `dist/How-Trellis-Works.pdf` |

Both HTML files are **generated** by `make_docs.py`, which holds the content and a
shared chassis so the two documents cannot drift apart. Edit the Python, not the HTML.

```bash
./build/export_docs.sh      # regenerates the HTML, exports both PDFs, verifies them
```

That runs headless Chrome, then `harden_pdf.py` and `finish_pdf.py` (Chrome's own export
reaches about 102 of 106), then checks each output twice: veraPDF for PDF/UA-1, and
`deck-a11y/scripts/verify_pdf_a11y.py` for the things conformance has no rule about.

**Keep the source here, in the repo.** The first versions of these documents were written
straight into a session scratchpad, which is temporary: the directory was cleared between
sessions, both sources were lost, and two shipped PDFs could no longer be rebuilt or even
corrected. Anything that ends up in `dist/` needs its input tracked here.

## Things that will silently break the export

Chrome's print-to-PDF drops most accessibility work unless the HTML is written for it.
`make_docs.py` explains each of these at the point it handles them:

- **Static font faces, one per weight.** Chrome cannot subset an instance of a variable
  font and falls back to Type3 glyph procedures with a mostly-zero `/Widths` array. It
  looks fine and extracts as garbage. Needs the static Atkinson Hyperlegible Next faces
  (Regular, SemiBold, Bold), read from `$BRAND_FONTS`, which defaults to
  `~/Documents/Sandbox/brand-fonts/atkinson-hyperlegible-next/static`; point it at any
  folder holding those three TTFs. `make_docs.py` refuses a variable file.
- **Only characters the font has.** Atkinson Hyperlegible Next has no arrows or ticks
  (→ ← ✓). Chrome fills a missing glyph from a system font at export, and some of those
  arrive as Type3. Write "then" or "to" instead. `<code>` and `<kbd>` are set in Menlo,
  which does have ⌘. `tests/run_tests.py --only brand` checks every character the
  guides draw against the embedded faces.
- **Real elements.** A `div` styled to look like a heading exports as a paragraph. Same
  for lists and tables.
- **Nothing load-bearing hidden.** `aria-hidden`, `sr-only` and off-screen positioning
  do not survive intact.
- **Letter-spacing at or below `.09em`.** At `.16em` text extracts as `I N T E RN A L`,
  and neither checker catches it.
- **No forced break before every section.** Eleven sections gave eleven half-empty
  pages. Atomic content carries `break-inside: avoid` instead.
