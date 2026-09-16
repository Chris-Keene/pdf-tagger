# PDF Tagger

A Mac app that takes a PDF with **no accessibility structure at all** — the kind
Google Slides, Keynote and most "print to PDF" routes produce — works out what
the structure probably is, walks you through confirming it, writes real tags into
the file, and then checks the result.

It is the missing half of `pdf-accessibility-check`. That skill *repairs* a tag
tree that already exists and correctly refuses a file that has none. This builds
one, with a human deciding the parts a script cannot know.

## Why this exists

An untagged PDF has words on the page and nothing else: no headings, no lists, no
reading order, no image descriptions. A screen reader gets a soup of text in
whatever order the exporter happened to paint it.

Post-processing alone cannot fix that, because the information is not in the file
to recover — which is exactly why the check-only skill refuses. What *is*
recoverable is a strong guess: font sizes rank headings, bullet glyphs mark list
items, indents give nesting, column geometry gives reading order. The app makes
that guess, shows it to you on top of the rendered page, and lets you correct it
before anything is written.

Measured on ten real Google Slides exports (1 to 20 pages): all ten reach
**veraPDF PDF/UA-1 106/106 rules, `isCompliant=true`**, and all ten render
**pixel-identical** to the original.

## Installing it

For handing out to non-technical people, see **[INSTALL.md](INSTALL.md)**, which
covers the Gatekeeper first-launch warning step by step. A distributable zip is at
`dist/PDF-Tagger-1.4.0.zip`.

## Using it

Double-click **PDF Tagger.app**. It opens a normal Mac window and walks five steps:

1. **Open** — drop a PDF in, or pick one.
2. **Document** — title and language. Both are required and both get read out.
3. **Structure** — every piece of content boxed on the rendered page, numbered in
   reading order. Change any role, move anything earlier or later.
4. **Images** — describe what carries meaning, mark the rest decorative.
5. **Export** — writes a new file, then validates it and tells you the truth
   about what it did and did not achieve.

Your original file is never modified. The tagged copy is always a new file.

Because it is not signed by a registered developer, the first launch needs
**right-click → Open** (or System Settings → Privacy & Security → Open Anyway).

You can also right-click a PDF and **Open With → PDF Tagger**, or drop one on the
Dock icon. It registers as an *alternate* handler for PDFs on purpose; hijacking
the default handler would be a rude thing for a tagging tool to do.

## Native window, and why it is shaped this way

The window is a real `NSWindow` with a real menu bar, native open and save panels,
and Dock and Cmd-Tab behaviour. Inside it, the UI is the same HTML in `app/ui`,
shown in a `WKWebView`.

That split is deliberate, and the alternatives are worse:

- **Rewriting the UI in AppKit or SwiftUI** would be weeks of work for the same
  five screens, and would leave the Python engine to be reached over some bridge
  anyway.
- **Rewriting the whole thing in Swift** is not really on the table: PDFKit
  cannot write a structure tree at all, so every bit of content-stream surgery
  would have to be rebuilt from scratch, losing pikepdf/qpdf and PyMuPDF.

So the native part is the shell, which is where "feels like a Mac app" actually
lives. The menu bar is not decoration either: without a real Edit menu, Cmd-C,
Cmd-V and Cmd-A do nothing inside a `WKWebView`'s text fields, which would make
writing image descriptions miserable.

Two details worth knowing if you touch it:

- Cocoa insists on owning the main thread, so the HTTP server moves to a daemon
  thread and the window drives the process. The server still exists, on
  `127.0.0.1` with a random port, it is just invisible.
- Panels have to run on the main thread while the request handler asking for one
  is on a worker thread, so `native.on_main` hops over and blocks until the user
  answers.

**Browser mode is kept as a fallback**, which is also what the test suite drives:

```bash
./.venv/bin/python run.py              # native window (default)
./.venv/bin/python run.py --browser    # open in the default browser instead
./.venv/bin/python run.py 8146 --no-browser   # server only, no UI
```

If PyObjC is missing for any reason, it says so in the log and falls back to the
browser rather than failing to start.

## What it can and cannot do

**It can:** build a structure tree from scratch; set headings, paragraphs, lists
with nesting, figures with alt text, and links; set language, title and the
document-level flags; wrap decoration as artifacts; fix reading order; reach full
PDF/UA-1 conformance.

**It cannot:**

- **Rescue broken text.** If the source embedded a variable font, the text
  extracts as "Mo ile" / "accessi ility" and no amount of tagging repairs it. The
  app checks for this on export and says so, because a conformance checker will
  not — PDF/UA has no rule for text-extraction integrity. Fix it in the source.
- **Judge whether a description is any good.** `/Alt="image"` passes every
  checker and tells a reader nothing. The app flags filenames, file paths, junk
  words and duplicates, but the words are yours.
- **Know the right reading order.** It infers order from columns and position,
  which is right most of the time and wrong on busy slides. PDF/UA has **no rule**
  for sensible reading order, so nothing downstream will catch a mistake here.
  This is the step worth your attention.
- **Reach text stored inside a form XObject.** It says so rather than pretending;
  recursing into forms is the obvious next piece of work.
- **Rebuild tables as tables.** Table cells come through as text, not `TD`/`TH`.
  A real data table needs a route that keeps the table structure.
- **Recover text baked into an image.** A screenshot of text is pixels. The words
  have to go in the description, or the slide needs remaking.

Conformance is a floor, not proof the document reads well. Both things are worth
saying to whoever receives the file.

## Running from source

```bash
python3.11 -m venv .venv
./.venv/bin/pip install pikepdf pymupdf pyobjc-framework-WebKit
./.venv/bin/python run.py            # opens in your browser
```

Dependencies: `pikepdf`, `pymupdf`, and `pyobjc-framework-WebKit` for the native
window (about 6MB of wheels; without it the app still runs in the browser).

Optional but recommended: `brew install verapdf` for independent conformance
validation. Without it the app still tags and exports, and says plainly that
conformance was not verified.

### Headless / batch

```bash
./.venv/bin/python scripts/tag_cli.py in.pdf -o out.pdf --alt "placeholder" --report
```

`--report` runs veraPDF. Note veraPDF **exits non-zero on FAIL**, so never chain
it with `&&`.

### Rebuilding the app

```bash
./build/make_app.sh
```

Needs `pyinstaller` in the venv. Output lands in `build/dist/`.

## How it works

Two views of each page are combined:

- **`pymupdf`** gives the semantic view: text grouped into blocks, lines and
  spans with font sizes and boxes. This decides what is a heading.
- **A content-stream walk** (`pikepdf`) gives the mechanical view: the exact
  operator ranges that paint each mark, with positions resolved through the
  graphics-state stack. This is what the tagger has to wrap.

The two are joined on position. Then every mark on the page is wrapped either in
a marked-content sequence with an MCID or in `/Artifact BMC … EMC`, because
PDF/UA 7.1 requires that nothing is left unmarked. A `/StructTreeRoot` is built
over those MCIDs with a `/ParentTree` mapping each one back to its element.

Rewriting goes through pikepdf's instruction tokeniser rather than raw bytes,
which sidesteps the trap that `BDC`/`BMC` are *postfix* operators — splitting a
stream at the keyword corrupts it in a way that looks like a semantic problem.

| file | what it does |
|---|---|
| `app/analyse.py` | PDF → proposed structure (roles, order, nesting, columns) |
| `app/tagger.py` | confirmed structure → tagged PDF (stream surgery + tree) |
| `app/hardening.py` | `/Lang`, `/MarkInfo`, `DisplayDocTitle`, XMP |
| `app/checks.py` | what still needs a human, in plain English |
| `app/server.py` | local HTTP API, stdlib only |
| `app/native.py` | the native window, menus and file panels (PyObjC) |
| `app/ui/index.html` | the five-step UI, single file, no network calls |
| `build/make_icon.py` | draws the app icon (no extra dependency) |

## Tests

```bash
./.venv/bin/python tests/run_tests.py            # everything, a few minutes
./.venv/bin/python tests/run_tests.py --quick    # 3 corpus files
./.venv/bin/python tests/run_tests.py --only trip,edge
./.venv/bin/python tests/run_tests.py --native-ui   # + the native window checks
```

The native window checks are opt-in because they briefly open a window.

### The corpus

Most of the suite runs on fixtures it builds itself. A further set of checks runs
against real PDFs, because the documents that break a detector are odd real ones
rather than anything a person would think to construct. Those files belong to
whoever made them, so none of them are in this repository, and every check that
needs one is skipped and reported rather than failed when it is missing.

Copy `tests/corpus.json.example` to `tests/corpus.json` and point it at your own:

| key | what it wants |
| --- | --- |
| `DIR` | a folder of untagged exports to sweep (default `~/Downloads`) |
| `DECK` | any real slide deck, for heading and quotation checks |
| `MISTAGGED` | an export whose existing tags are wrong, e.g. from Google Docs |
| `SHADED_TABLE` | a document with a borderless, shaded table |
| `TAGGED` | glob patterns for files some other tool already tagged |

`PDFTAG_CORPUS_<KEY>` overrides any of them. `DIR` is worth setting deliberately:
the default sweeps a whole Downloads folder, which is a rude thing to do to
somebody silently.

Two fixtures work the same way. `tests/slides-6page.pdf` and
`tests/slides-1page.pdf` are real exports if you drop them in; without them the
suite builds a deck of the same shape and carries on.

Exits non-zero on failure. Seven groups: the real Slides corpus, round-trip,
files already tagged by other tools, edge cases, fonts, nested text, error
messages, and the HTTP API.

Two invariants are worth calling out, because they are what the suite is really
built around:

- **Tagging must never introduce a conformance failure.** Asserting "the output
  is compliant" instead would fail on sources with their own defects — a font the
  producer never embedded cannot be fixed by tagging — so the suite diffs
  veraPDF's failed rules before and after and requires the delta to be empty.
- **Tagging must be repeatable.** Re-opening a tagged file and tagging it again
  produces **byte-identical** content streams. Before this was enforced, each
  pass nested its marked content inside the previous one: the stream grew every
  time and MCIDs were duplicated, which the spec forbids and which **veraPDF
  reports as compliant anyway**.

The suite also carries a deeper structural check than veraPDF performs, because
several real defects sail past conformance: duplicate or unbalanced
marked-content ids, nested marked content, MCIDs with no owner in the ParentTree,
`/P` links that do not point at the real parent, empty `/Alt`, and skipped
heading levels.

## Small controls

One treatment was doing six different jobs. On a single structure-step screen there
were **66 small buttons, all rendered identically**: page navigation, view toggles,
zoom, help topics, reordering, and the one action that resolves a review item. A
pressed toggle also used the same navy fill as the primary button in the footer, so
an engaged filter looked like the main call to action.

They are now typed by what they do:

| variant | job | treatment |
|---|---|---|
| `.commit` | resolves a review item | filled green, tick prefix — matches the "checked" badge it produces |
| `.toggle` | on/off | state pip, tinted when engaged; no longer navy |
| `.seg` | related view controls | segmented cluster (zoom, page nav) rather than loose pills |
| `.step` | reorder, two per row | quietest thing on screen: small, muted, arrows only |
| `.info` | explains rather than acts | dashed and tinted |
| `.jump` | takes you elsewhere | link-like, arrow suffix |

Two things this exposed, both from making controls *quieter*:

- **A faint border is not a quiet control, it is an unidentifiable one.** `--line`
  is a decorative rule colour at 1.44:1; a control boundary needs 3:1 (WCAG 2.2
  1.4.11). Quiet now comes from size and ink, with borders at 3.4–4.5:1.
- **Raising the page-rail resting border to 3:1 left it only 1.13:1 against the
  pink current-page border**, so that state stopped being distinguishable by hue.
  It now differs in *form* too — 3px against 2px, plus a halo.

Audited across 154 controls: no text, border or target-size failures.

## The page preview

The structure step is a working canvas, not prose, so it drops the reading-width
cap the other steps use and gives the page preview the larger share of the split.
On a 1500px window that took the preview from 712px to 863px.

There is a **zoom control** above it (100% to 400%, plus fit-to-width) for slides
where boxes sit close together — a wall of customer logos, say, where at fit width
you cannot tell which box covers which mark. The overlay boxes are positioned in
percentages, so scaling the stage scales them for free; the page image is
re-requested at a matching resolution so zooming in does not just magnify blur.

Once zoomed, the preview scrolls, drags, and takes arrow keys, and selecting an
item centres it in view. Two things worth knowing if you touch that centring:

- **A scroll assignment is clamped to the element's current scrollable range.**
  Writing `scrollTop` before the page image has loaded is silently rounded to 0,
  because the stage has almost no height yet.
- **`requestAnimationFrame` does not fire when the page is not painting** (a
  background tab, or a headless context), so the callback simply never ran. It uses
  `setTimeout` instead.

## Confidence, and reviewing what was guessed

Every proposed role carries a **0-100 confidence** and a sentence saying why, and
the score is not decoration: it decides what the user is asked to look at.

It is scored on the strength of the evidence *and* on what a wrong call would
cost. A line with a list marker is 95. Text set at the body size is 95. A line 60%
bigger than the body text is a confident heading; one 13% bigger is barely over
the line and scores near 68. A bold two-word label that is *not* bigger than the
body text is 62, because it is usually a subheading and sometimes not. Anything
sitting between two columns loses points, because its place in the reading order
is a guess and reading order is the one thing no checker will ever question.

Weighting by cost matters. An early version scored "smaller than body text" as
low confidence, which flagged **43% of one deck** — mostly paragraph-versus-caption
calls that change nothing for a screen reader, drowning out the
paragraph-versus-heading calls that change everything. Tuned, it flags **11%**
across the test corpus, 0-27% per file.

Below 70 an item is queued for review, and the user can either change the role or
confirm it with **Looks right**. Changing a role counts as confirming. Confirmed
items stop being asked about. The page rail counts what is still open per page,
the overlay flags uncertain items with a marker, there is a filter to show only
those, and a button steps through them across pages.

Images are deliberately **not** in that list. They have their own step, where the
real question is the description rather than the role, and counting them in both
places made both lists less useful.

### Decisions are remembered

Re-opening a file this app tagged reads the roles back out of its structure tree
instead of guessing again, so **a role the user changed survives the round trip**
and comes back marked as checked. Before this, the app reverted the user's changes
on re-upload and asked them to review everything a second time, which made
confirming a proposal pointless.

Provenance decides how far the tags are trusted. Ours (identified by
`xmp:CreatorTool`) means a human already worked through the document, so they are
treated as settled. Another tool's are adopted too — better evidence than a guess
from font sizes — but nobody has vouched for them, so they are used without being
signed off, and the user is told once at the document level rather than being
handed several hundred individual items to confirm.

Anything settled is also exempt from heading re-levelling. Overruling a decision
someone made on purpose is worse than leaving a gap in the heading levels and
telling them about it.

## Three things the app now refuses to fake

Each of these produces a file that would otherwise look tagged while being
useless, so the app reports them instead of papering over them.

- **Text inside a form XObject.** Some tools wrap page content in a nested
  object. A page-level pass cannot reach it, and the tempting move — artifacting
  the `Do` — marks the whole document as decoration, which *satisfies* the
  unmarked-content rule and so passes conformance while hiding everything from a
  screen reader. It is left unmarked instead, so the file fails honestly, and the
  user is told up front.
- **Fonts the producer never embedded.** Nothing tagging does can add the missing
  glyphs. Reported before the user spends time on the later steps. Type 3 fonts
  are explicitly *not* counted here: their glyphs are procedures in `/CharProcs`,
  so there is no font file to embed. They get their own note, because they are
  often the signature of the variable-font export that garbles text.
- **Marked content left behind by another tool.** Stripped from page streams *and*
  from inside form XObjects. Missing the latter is what put already-tagged content
  inside an artifact and failed 7.1 t2.

## Accessibility of the app itself

Audited in the browser rather than by eye: computed contrast on every visible text
node (including `aria-hidden` labels, which sighted users still read), computed
accessible names from the real accessibility tree, heading sequence, keyboard
focus order and visible focus, and WCAG 2.2 target sizes. Zero contrast
failures, no unnamed controls, no skipped heading levels.

Two things worth recording:

- **Brand pink `#F20487` carries white text at only 4.08:1.** Buttons and labels
  use the derived `#D10375` (5.28:1); `#F20487` is kept for non-text only, where
  the 3:1 threshold applies. Same fix in both directions, since contrast is
  symmetric.
- **The overlay boxes are deliberately smaller than 24×24px** where the content
  they trace is smaller. Their geometry has to match the page exactly, which is
  WCAG 2.2 2.5.8's "essential" exception, and every box has a full-size
  equivalent control in the item list beside it, which is the other exception.

## Notes for whoever maintains this

- **The analyser's heuristics are the interesting part**, and they were all
  written against real decks. Body text size is judged across the whole document
  (a title slide has no body text, so a per-page estimate makes the title
  "body"). A bullet glyph is often set at a different size from its own text
  (11pt bullet, 14pt words), so the size test is suspended while an item is still
  waiting for its words. Heading levels are assigned with a stack so the sequence
  never skips.
- **Slides paints every image twice** at the same position. The second draw is
  artifacted, or you get asked to describe the same picture twice.
- **Image matrices are flipped.** Take the bounding box by transforming the unit
  square through the CTM; treating `(tx, ty)` as a corner puts a full-bleed
  footer graphic at the top of the page.
- **Two separate bugs both made the app "listen but never accept"** — the socket
  binds early, so anything that blocks or raises afterwards leaves a process that
  looks perfectly alive while every request hangs. Worth knowing the shape of it:
  - **PyInstaller `--windowed` gives `sys.stdout is None`**, so a bare `print()`
    raises. Everything goes through `log()`, and `run.py` installs a null sink.
  - **`HTTPServer.server_bind()` calls `socket.getfqdn()`** just to fill in
    `server_name`, which nothing reads. In the frozen bundle that reverse DNS
    lookup blocks for **35 seconds**. `Server.server_bind` skips it: startup went
    from 35,238ms to 559ms.
- **The header logo is embedded as a WebP data URI**, because the UI is a single
  self-contained file with no network access. It is downscaled to the 22px it
  displays at and embedded at 3x for high-DPI, 7.2KB.
- **Grid tracks are all `minmax(0, …)`.** A bare `1fr` is `minmax(auto, 1fr)`,
  whose auto minimum is min-content, so the wide page rail pushed its column past
  the window and the panel overflowed sideways below about 1000px.
- The app is unsigned and unnotarised. Fine internally; distributing it more
  widely means a Developer ID and notarisation.
