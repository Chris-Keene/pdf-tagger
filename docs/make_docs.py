#!/usr/bin/env python3
"""Build the two shipped guides as single-file HTML, ready for export to PDF.

Lives in the repo deliberately. The first versions of these documents were written
straight into a session scratchpad, which is temporary: the directory was cleared,
both sources were lost, and two shipped PDFs could no longer be rebuilt or even
corrected. Anything that ends up in dist/ needs its input tracked here.

The chassis is shared so the two documents cannot drift apart, and it is built to
survive Chrome's print-to-PDF with its accessibility intact:

  * static font faces, one per weight, embedded as data URIs. Chrome cannot subset
    an instance of a variable font and falls back to Type3 glyph procedures with a
    mostly-zero /Widths array, which garbles every tool that reconstructs reading
    order from glyph advances.
  * real h1/h2/h3, ul/li, table/th/caption. A div styled to look like a heading
    exports as a paragraph.
  * nothing load-bearing inside aria-hidden or positioned off-screen.
  * letter-spacing kept at or below .09em. At .16em the tracking is wide enough
    that text extracts as "I N T E RN A L", and neither veraPDF nor the practical
    checker catches it.
  * each numbered section starts a new page, so nothing overruns.

Usage:  python3 docs/make_docs.py          # writes both .html next to this file
"""

import base64
import os
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
FONT_DIR = pathlib.Path.home() / "Library" / "Fonts"
FACES = [("Outfit-Regular.ttf", 400), ("Outfit-SemiBold.ttf", 600),
         ("Outfit-Bold.ttf", 700)]

VERSION = "1.19.0"
ZIP_NAME = f"PDF-Tagger-{VERSION}.zip"
ZIP_SIZE = "89MB"


def font_faces():
    out = []
    for name, weight in FACES:
        path = FONT_DIR / name
        if not path.exists():
            raise SystemExit(
                f"missing {path}. Install the static Outfit faces (not the "
                f"variable font) before building.")
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        out.append(
            "@font-face{font-family:Outfit;font-style:normal;font-weight:%d;"
            "font-display:block;src:url(data:font/ttf;base64,%s) format('truetype')}"
            % (weight, b64))
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Shared chassis
# --------------------------------------------------------------------------- #

CSS = """
:root{
  /* chriskeene.me: deep sky blue (Capri) on white, Outfit throughout. Values are
     lifted from the site rather than re-derived so the two cannot drift.
     --accent is the brand hue and is used for rules, strips and fills under dark
     text only: white on it is 2.12:1. --accent-ink is the text-safe variant at
     6.25:1 on white. */
  --navy:#006699; --ink:#0B1F2A; --muted:#51687A; --bg:#F2F9FE;
  --accent:#00BFFF; --accent-soft:#D6EEFB; --heading:#0086C7;
  --pink:#C6106E; --pink-ink:#C6106E; --link:#006699;
  --line:#CDE7F7; --white:#FFFFFF;
  --ok:#1E7A45; --todo:#8A5A00;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--white);color:var(--ink);
  font:400 10.5pt/1.55 Outfit,"Helvetica Neue",Arial,sans-serif}
code,kbd{font-family:Menlo,Consolas,"DejaVu Sans Mono",monospace;font-size:.92em}

/* ---- page furniture ---- */
.sheet{max-width:170mm;margin:0 auto;padding:14mm 0 10mm}
.strip{height:3mm;background:var(--accent);border-radius:2px;
  margin:0 0 6mm}
.eyebrow{font:600 8pt/1.3 Outfit,sans-serif;letter-spacing:.09em;
  text-transform:uppercase;color:var(--navy);margin:0 0 2mm}
h1{font:700 25pt/1.15 Outfit,sans-serif;color:var(--navy);margin:0 0 4mm;
  letter-spacing:-.01em}
.lede{font-size:11.5pt;color:var(--ink);margin:0 0 6mm;max-width:150mm}
h2{font:700 14pt/1.25 Outfit,sans-serif;color:var(--navy);
  margin:0 0 3mm;display:flex;align-items:baseline;gap:3mm}
h2 .n{font:700 9pt/1 Outfit,sans-serif;color:var(--white);background:var(--navy);
  border-radius:3px;padding:1.4mm 2mm;letter-spacing:.06em;flex:none}
h3{font:600 11pt/1.3 Outfit,sans-serif;color:var(--navy);margin:5mm 0 2mm}
p{margin:0 0 3mm;max-width:150mm}
strong{font-weight:600}
a{color:var(--link)}
ul,ol{margin:0 0 3mm;padding-left:6mm;max-width:150mm}
li{margin:0 0 1.6mm}
/* Sections flow rather than each taking a fresh page. Forcing a break before
   every one of eleven sections gave eleven pages, most of them half empty. What
   actually caused the overrun this replaced was atomic content being split, so
   those pieces are what carry break-inside:avoid, and a heading is never left
   stranded at the foot of a page. */
section{break-inside:auto}
h2{break-after:avoid;break-inside:avoid}
h3{break-after:avoid}
.foot{break-before:avoid}

/* ---- facts strip ---- */
.facts{display:flex;flex-wrap:wrap;gap:0;border:1px solid var(--line);
  border-radius:5px;overflow:hidden;margin:0 0 7mm}
.facts div{flex:1 1 40mm;padding:3mm 4mm;border-right:1px solid var(--line)}
.facts div:last-child{border-right:0}
.facts b{display:block;font:600 8pt/1.3 Outfit,sans-serif;color:var(--muted);
  letter-spacing:.07em;text-transform:uppercase;margin-bottom:.8mm}
.facts span{font-size:10pt;color:var(--navy);font-weight:600}

/* ---- callouts ---- */
.note{background:var(--bg);border-left:3px solid var(--navy);
  border-radius:0 4px 4px 0;padding:3.5mm 4mm;margin:0 0 4mm;break-inside:avoid}
.note h3{margin:0 0 1.5mm}
.note p:last-child{margin-bottom:0}
.warn{background:#FFFBF2;border-left-color:var(--todo)}
.win{background:#F2FAF5;border-left-color:var(--ok)}

/* ---- numbered steps ---- */
ol.steps{list-style:none;padding:0;counter-reset:s;margin:0 0 5mm}
ol.steps>li{counter-increment:s;position:relative;padding:0 0 3.5mm 11mm;
  break-inside:avoid}
ol.steps>li::before{content:counter(s);position:absolute;left:0;top:0;
  width:7mm;height:7mm;border-radius:50%;background:var(--navy);color:var(--white);
  font:700 9pt/7mm Outfit,sans-serif;text-align:center}
ol.steps b{display:block;font-weight:600;color:var(--navy)}

/* ---- tables ---- */
.tablewrap{margin:0 0 5mm}
table{border-collapse:collapse;width:100%;font-size:9.5pt}
caption{text-align:left;font:600 9pt/1.4 Outfit,sans-serif;color:var(--muted);
  padding:0 0 1.5mm;letter-spacing:.04em;text-transform:uppercase}
th,td{border:1px solid var(--line);padding:2.2mm 2.6mm;text-align:left;
  vertical-align:top}
thead th{background:var(--navy);color:var(--white);font-weight:600;
  font-size:8.5pt;letter-spacing:.05em;text-transform:uppercase}
tbody th{background:var(--bg);font-weight:600;color:var(--navy);width:38mm}
thead{display:table-header-group}
tr{break-inside:avoid}

/* ---- chips ---- */
.chip{display:inline-block;font:600 8pt/1 Outfit,sans-serif;padding:1.4mm 2.2mm;
  border-radius:3px;letter-spacing:.05em;text-transform:uppercase;white-space:nowrap}
.chip.ok{background:#D9F0E3;color:#125C32}
.chip.todo{background:#FBEEd7;color:#6E4700}

/* ---- code ---- */
pre{background:var(--navy);color:#EAF0F8;border-radius:4px;padding:3mm 4mm;
  margin:0 0 4mm;font-size:9pt;overflow-x:auto;white-space:pre-wrap;
  word-break:break-word;break-inside:avoid}
p code{background:var(--bg);border:1px solid var(--line);border-radius:3px;
  padding:.3mm 1.2mm}

/* ---- figures ---- */
figure{margin:0 0 5mm;break-inside:avoid}
figure svg{width:100%;height:auto;display:block}
figcaption{font-size:9.5pt;color:var(--muted);margin-top:2mm}

/* ---- footer ---- */
.foot{border-top:1px solid var(--line);margin-top:7mm;padding-top:3mm;
  font-size:9pt;color:var(--muted)}

@page{size:A4;margin:16mm 18mm}
@media print{
  body{font-size:10pt}
  .sheet{max-width:none;padding:0}
  a{color:var(--link);text-decoration:underline}
}
"""


def page(title, eyebrow, h1, lede, facts, body, foot):
    rows = "".join(f"<div><b>{k}</b><span>{v}</span></div>" for k, v in facts)
    return f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
{font_faces()}
{CSS}
</style>
</head>
<body>
<main class="sheet">
<div class="strip" aria-hidden="true"></div>
<p class="eyebrow">{eyebrow}</p>
<h1>{h1}</h1>
<p class="lede">{lede}</p>
<div class="facts">{rows}</div>
{body}
<p class="foot">{foot}</p>
</main>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# 1. Installing PDF Tagger  (for the person receiving the app)
# --------------------------------------------------------------------------- #

INSTALL_BODY = f"""
<section>
<h2><span class="n">01</span>Install it</h2>
<ol class="steps">
  <li><b>Unzip the file</b>Double-click <code>{ZIP_NAME}</code>. You will get an app
      called PDF Tagger.</li>
  <li><b>Drag it into Applications</b>Not strictly required, but it is where you will
      look for it next week, and apps left in Downloads can behave oddly.</li>
  <li><b>Open it, and expect to be stopped once</b>The first double-click will be
      blocked by macOS. Nothing is wrong with the app. The panel overleaf clears it
      for good.</li>
</ol>
<div class="note">
  <h3>What you need</h3>
  <p>A Mac with Apple silicon &mdash; an M1 or later, so anything bought since roughly
     2021. Nothing else to install, no sign-in, and it works offline. On an older Intel
     Mac this build will not open at all: check <strong>Apple menu &rarr; About This
     Mac</strong>, and if it says Intel, ask for a build that suits.</p>
</div>
</section>

<section>
<h2><span class="n">02</span>&ldquo;macOS cannot check it for malicious software&rdquo;</h2>
<p>You will see this the first time you open the app, and only the first time.</p>
<ol class="steps">
  <li><b>Double-click PDF Tagger</b>A message appears saying macOS cannot verify it.
      Click <strong>Done</strong> (or OK, or Cancel &mdash; whichever it offers).</li>
  <li><b>Open System Settings</b>The grey cog in your Dock, or press
      <kbd>&#8984;</kbd> + <kbd>Space</kbd> and type &ldquo;System Settings&rdquo;.</li>
  <li><b>Click Privacy &amp; Security</b>In the left-hand list.</li>
  <li><b>Scroll down to Security</b>There will be a line saying &ldquo;PDF Tagger was
      blocked to protect your Mac.&rdquo;</li>
  <li><b>Click Open Anyway</b>Then confirm with your password, Touch ID or Apple
      Watch.</li>
  <li><b>Done</b>The app opens, and from now on it opens like anything else.</li>
</ol>
<div class="note">
  <h3>Why does this happen?</h3>
  <p>Apple charges an annual fee to register apps so they open without a warning. This
     is an internal tool that has not been through that, so macOS treats it as an
     unknown developer. It is not a sign that anything is wrong with the file.</p>
</div>
<div class="note warn">
  <h3>Faster route, if you are comfortable with Terminal</h3>
  <p>This skips the whole panel above. Open Terminal, paste the line, press Return,
     then open the app normally.</p>
  <pre>xattr -d -r com.apple.quarantine "/Applications/PDF Tagger.app"</pre>
  <p>If you put the app somewhere other than Applications, change the path to match.</p>
</div>
</section>

<section>
<h2><span class="n">03</span>Using it</h2>
<p>The app opens filling the screen, and works through five steps along the top of the
   window. Each one explains itself as you go.</p>
<ul>
  <li><strong>Open a PDF.</strong> Drop one onto the window, or click
      <strong>Choose a PDF</strong>. You can also right-click any PDF in Finder and pick
      <strong>Open With &rarr; PDF Tagger</strong>.</li>
  <li><strong>Leave &ldquo;Help tips on&rdquo;</strong> while you are learning. When the
      explanations start getting in the way, switch them off with the button in the top
      right.</li>
  <li><strong>Your original file is never changed.</strong> The last step saves a new,
      tagged copy alongside it.</li>
  <li><strong>On a longer document it goes page by page.</strong> The strip of
      thumbnails shows what is still outstanding on each one, and the button at the
      bottom right moves to the next page rather than skipping to the end.</li>
</ul>
<h3>Optional: let Claude help with the two hard parts</h3>
<p>Two jobs in this app cannot be done by a script: writing a description for each
   image, and judging whether a page reads in a sensible order. If you have Claude
   Code installed and signed in, the app can offer both. It is
   <strong>off until you switch it on</strong>, per document.</p>
<ul>
  <li><strong>Drafting descriptions</strong> sends a crop of each image, and the
      text near it, to Claude. Not the whole page, and not the rest of the
      document.</li>
  <li><strong>Checking reading order</strong> sends only the words and where they
      sit. No image ever leaves the machine for this one.</li>
  <li><strong>Nothing is accepted for you.</strong> A drafted description arrives
      marked as a draft and still counts as outstanding until you have read it
      against the picture. That is deliberate: a description that reads well and
      describes the wrong thing passes every automated check and helps nobody.</li>
  <li><strong>Think before using it on customer material.</strong> Switching it on
      means content leaves this Mac. For a deck you would not paste into a chat
      window, leave it off and write the descriptions yourself.</li>
</ul>

<h3>Fixing what it gets wrong</h3>
<p>Most of the work is confirming what the app proposes. Where it has grouped something
   badly, the tools are on the item itself:</p>
<ul>
  <li><strong>Drag the handle</strong> &mdash; the grip of small dots at the left of
      each row &mdash; to move an item in the reading order, or use the up and down
      arrow buttons. Both do the same thing.</li>
  <li><strong>Split</strong> breaks one item into separate pieces &mdash; for when two
      things sitting side by side, like table cells, have been counted as one.</li>
  <li><strong>Join up</strong> does the reverse, for a sentence that has been chopped in
      half.</li>
  <li><strong>Click anything on the page preview</strong> to jump straight to it in the
      list, and zoom in if the boxes are too close together to tell apart.</li>
</ul>
</section>

<section>
<h2><span class="n">04</span>If something goes wrong</h2>
<div class="tablewrap">
<table>
  <caption>Common messages and what they mean</caption>
  <tbody>
    <tr><th>It will not open even after &ldquo;Open Anyway&rdquo;</th>
        <td>Move it into your Applications folder and try once more.</td></tr>
    <tr><th>It says the PDF is password protected</th>
        <td>It is. Remove the password on the original, then try again.</td></tr>
    <tr><th>It says some of the text cannot be tagged</th>
        <td>Some PDFs store text in a way this app cannot reach. It tells you rather
            than pretending it worked. The fix is to re-export the document from
            wherever it was made.</td></tr>
    <tr><th>It says a font is not stored in the PDF</th>
        <td>Same kind of thing: no amount of tagging can add a missing font. It has to
            be re-exported with the fonts embedded.</td></tr>
    <tr><th>It warns the tagged copy will not be password protected</th>
        <td>Correct. Tagging has to rewrite the file and it comes out unprotected. The
            content is identical; re-apply the password to the copy if it matters.</td></tr>
    <tr><th>Anything else</th>
        <td>There is a log at <code>~/Library/Logs/PDF&nbsp;Tagger.log</code>. Send that
            on with a note about what you were doing.</td></tr>
  </tbody>
</table>
</div>
</section>

<section>
<h2><span class="n">05</span>Distribution notes</h2>
<p class="eyebrow">For whoever is handing this out</p>
<ul>
  <li><strong>Send <code>{ZIP_NAME}</code> ({ZIP_SIZE}).</strong> Zipped with
      <code>ditto</code>, so the code signature survives the transfer. Slack, Drive and
      email are all fine at that size.</li>
  <li><strong>Everyone will hit the Gatekeeper warning.</strong> The app is ad-hoc
      signed rather than signed with a Developer ID. Verified: a received copy picks up
      the quarantine flag and <code>spctl</code> rejects it.</li>
  <li><strong>On macOS 15 the Control-click &rarr; Open shortcut no longer works</strong>
      for this, which is why the instructions above use System Settings. Sending people
      the old right-click trick will fail at the first step.</li>
  <li><strong>To remove the friction entirely</strong> the app needs a Developer ID and
      notarisation: an Apple Developer account at roughly &pound;79 a year, then signing
      and stapling the build. After that it opens with no warning and these instructions
      shrink to &ldquo;unzip, drag, open&rdquo;. Worth it past a handful of people;
      overkill for a pilot.</li>
  <li><strong>Apple silicon only.</strong> The build is arm64, so an Intel Mac cannot
      run it. A universal build would need universal2 wheels for pikepdf and PyMuPDF
      &mdash; doable, not free.</li>
  <li><strong>veraPDF is bundled</strong>, so recipients get the real conformance verdict
      without installing Java or Homebrew &mdash; the veraPDF CLI plus a
      <code>jlink</code>-trimmed OpenJDK runtime, verified to give a verdict identical to
      a system install with no Java on the PATH. It accounts for 63MB of the download;
      <code>--no-verapdf</code> builds a 45MB version that tags identically but shows no
      pass or fail.</li>
  <li><strong>Licensing for the redistributed parts:</strong> veraPDF is dual
      GPL-3.0-or-later / MPL-2.0, included under MPL-2.0 (file-level copyleft, nothing
      imposed on our source). The runtime is OpenJDK under GPLv2 with the Classpath
      Exception, which is what permits shipping a trimmed runtime inside an app. Licence
      texts ship in <code>Contents/Resources/verapdf/</code>.</li>
  <li><strong>Nothing is installed, and nothing leaves the machine unless somebody
      switches it on.</strong> The app runs a local server on 127.0.0.1 for its own
      window to talk to, on a random port. The one exception is the optional Claude
      help described below, which is off until the user turns it on, per document.</li>
</ul>
</section>
"""

INSTALL = page(
    title="Installing PDF Tagger",
    eyebrow="chriskeene.me &middot; PDF Tagger",
    h1="Installing PDF Tagger",
    lede="Takes about a minute. There is one step where macOS will refuse to open the "
         "app &mdash; that is expected, and step 3 gets you past it for good.",
    facts=[("Version", f"PDF Tagger {VERSION}"),
           ("Needs", "macOS 12+, Apple silicon"),
           ("Download", f"{ZIP_NAME} &middot; {ZIP_SIZE}"),
           ("Network", "None. Works offline")],
    body=INSTALL_BODY,
    foot=f"PDF Tagger {VERSION} &middot; macOS 12 or later, Apple silicon. Turns a PDF "
         "with no accessibility structure into a tagged one, with you confirming the "
         "parts a script cannot know.")


# --------------------------------------------------------------------------- #
# 2. How PDF Tagger Works  (engineering handover)
# --------------------------------------------------------------------------- #

JOIN_SVG = """
<figure>
<svg viewBox="0 0 640 190" role="img" aria-label="A PDF page is parsed two ways at
once. A semantic view from MuPDF gives words, font sizes and boxes, so it knows a line
is 32pt and therefore a heading. A mechanical view from qpdf gives operator ranges and
positions, so it knows which drawing instructions paint that line. The two are matched
on position to produce an element that carries both a meaning and a handle to rewrite
it.">
  <defs>
    <marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7"
            markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="currentColor"/>
    </marker>
  </defs>
  <g fill="none" stroke="currentColor" stroke-width="1.4">
    <rect x="1" y="70" width="96" height="48" rx="4"/>
    <rect x="196" y="6" width="200" height="62" rx="4"/>
    <rect x="196" y="120" width="200" height="62" rx="4"/>
    <rect x="496" y="60" width="140" height="68" rx="4" stroke="#D10375"
          stroke-width="2"/>
    <path d="M99,86 L192,40" marker-end="url(#ar)"/>
    <path d="M99,102 L192,148" marker-end="url(#ar)"/>
    <path d="M398,40 L446,86" marker-end="url(#ar)"/>
    <path d="M398,148 L446,102" marker-end="url(#ar)"/>
    <path d="M446,94 L492,94" marker-end="url(#ar)"/>
  </g>
  <g font-family="Outfit, sans-serif" fill="currentColor">
    <text x="49" y="90" font-size="12" font-weight="600" text-anchor="middle">PDF</text>
    <text x="49" y="105" font-size="12" font-weight="600" text-anchor="middle">page</text>
    <text x="145" y="52" font-size="10" text-anchor="middle">parse</text>
    <text x="145" y="142" font-size="10" text-anchor="middle">walk</text>
    <text x="208" y="26" font-size="11" font-weight="700">Semantic view &middot; MuPDF</text>
    <text x="208" y="43" font-size="10">words, font sizes, boxes</text>
    <text x="208" y="59" font-size="10" fill="#595959">&ldquo;32pt, so a heading&rdquo;</text>
    <text x="208" y="140" font-size="11" font-weight="700">Mechanical view &middot; qpdf</text>
    <text x="208" y="157" font-size="10">operator ranges + positions</text>
    <text x="208" y="173" font-size="10" fill="#595959">&ldquo;ops 73&ndash;79 paint it&rdquo;</text>
    <text x="452" y="26" font-size="9.5" text-anchor="middle">joined on</text>
    <text x="452" y="38" font-size="9.5" text-anchor="middle">position</text>
    <text x="566" y="86" font-size="12" font-weight="700" text-anchor="middle"
          fill="#D10375">Element</text>
    <text x="566" y="103" font-size="10" text-anchor="middle">a meaning, and a</text>
    <text x="566" y="117" font-size="10" text-anchor="middle">handle to rewrite</text>
  </g>
</svg>
<figcaption>The join is the whole trick. A layout engine knows a line is 32pt but not
which bytes drew it; a content-stream parser knows which operators drew it but not that
32pt means &ldquo;heading&rdquo;. Verified against real files: positions from the two
views agree to the baseline.</figcaption>
</figure>
"""

WORKS_BODY = f"""
<section>
<h2><span class="n">01</span>The problem</h2>
<p>A PDF exported from Google Slides, Keynote, or most &ldquo;print to PDF&rdquo; routes
   contains no accessibility structure whatsoever. No headings, no lists, no reading
   order, no image descriptions. The words are painted on the page and nothing records
   what any of them are.</p>
<p>For a screen reader user that is close to unusable: no way to skim by heading, no
   sense of how long a list is, no idea what the pictures show, and content delivered in
   whatever order the exporter happened to draw it.</p>
<p>It is not a rare edge case. A single laptop audit found 35 untagged Slides exports,
   including customer-facing audit results, exec decks and product one-pagers &mdash;
   documents already sent to customers who are buying accessibility.</p>
<div class="note">
  <h3>Why a script alone cannot fix it</h3>
  <p>Existing repair tooling assumes a structure tree exists and mends it. Here there is
     nothing to mend. Building one means deciding whether an 18pt bold line is a heading
     or a label, whether an image carries meaning, and which of three columns to read
     first. Those are judgements, not lookups.</p>
  <p>The unlock is not better inference. It is splitting the work: the machine does what
     is mechanical and reliable, and asks a person only where the answer genuinely is
     not derivable.</p>
</div>
</section>

<section>
<h2><span class="n">02</span>The mechanism that makes it possible</h2>
<p>To write a tag into a PDF you need two unrelated things at once: what a run of text
   means, and which drawing instructions paint it. No single library gives both, so the
   app builds two views of every page and joins them.</p>
{JOIN_SVG}
</section>

<section>
<h2><span class="n">03</span>What actually changes in the file</h2>
<p>Tagging wraps every mark on the page in a marked-content sequence carrying an id,
   then builds a structure tree over those ids. Anything decorative is wrapped as an
   artifact instead, because the standard requires that nothing is left unmarked.</p>
<div class="tablewrap">
<table>
  <caption>Before and after, for one heading</caption>
  <thead><tr><th>Before</th><th>After</th></tr></thead>
  <tbody>
    <tr>
      <td><code>BT /F2 32 Tf &hellip; TJ ET</code><br>no marker, no id<br>
          no structure tree in the file</td>
      <td><code>/H1 &lt;&lt;/MCID 0&gt;&gt; BDC</code><br>
          <code>BT /F2 32 Tf &hellip; TJ ET</code><br><code>EMC</code><br>
          same drawing ops, now identified, with a ParentTree mapping id to element</td>
    </tr>
  </tbody>
</table>
</div>
<div class="note win">
  <h3>Pixels unchanged, verified across 261 pages</h3>
  <p>Nothing visual is touched. The drawing operators are identical; only markers and a
     tree are added around them. That is why every output renders pixel-for-pixel
     identically to its input, which is the property that makes this safe to run on a
     document already approved by a customer.</p>
</div>
</section>

<section>
<h2><span class="n">04</span>The pipeline, and where the person sits</h2>
<p>Five stages. Only one of them needs a human, and it is deliberately the only one,
   because everything either side is deterministic and testable.</p>
<div class="tablewrap">
<table>
  <caption>The five stages</caption>
  <thead><tr><th>Stage</th><th>What happens</th></tr></thead>
  <tbody>
    <tr><th>Analyse</th><td>Scored proposals, with a plain-English reason for each</td></tr>
    <tr><th>Confirm</th><td>A person, only on what scored low &mdash; about 9% of items</td></tr>
    <tr><th>Repair</th><td>Split, join and reorder, for the grouping the geometry got wrong</td></tr>
    <tr><th>Write</th><td>A new file. The original is never modified</td></tr>
    <tr><th>Verify</th><td>veraPDF, plus the app's own text-integrity check</td></tr>
  </tbody>
</table>
</div>
<div class="note">
  <h3>Re-opening the file recovers the decisions</h3>
  <p>Roles and descriptions are read back out of the tags rather than guessed again.
     Without that, re-opening a file someone had already worked on threw their
     judgements away and asked for all of them again, which made confirming a proposal
     pointless.</p>
</div>
</section>

<section>
<h2><span class="n">05</span>How it decides what to ask</h2>
<p>Every proposed role carries a 0&ndash;100 confidence and a plain-English reason. Below
   70 the item is queued for review; the user confirms it or changes it, and it stops
   being asked about. Changing a role counts as confirming.</p>
<p>The scoring weights evidence strength against <strong>cost of being wrong</strong>,
   and the second half matters more than it sounds. An early version scored
   &ldquo;smaller than body text&rdquo; as low confidence and flagged 43% of one deck
   &mdash; almost all paragraph-versus-caption calls, which change nothing for a screen
   reader, drowning out the paragraph-versus-heading calls that change everything.
   Reweighted by consequence, it flags <strong>9%</strong> across the test corpus.</p>
<div class="tablewrap">
<table>
  <caption>Representative scores</caption>
  <thead><tr><th>Score</th><th>Evidence</th></tr></thead>
  <tbody>
    <tr><th>95 &middot; near-certain</th><td>A line beginning with a bullet glyph or a
        list marker. Text set at the document's body size.</td></tr>
    <tr><th>68&ndash;95 &middot; graded</th><td>Heading level by how far clear of the
        size boundary it sits. 60% larger is confident; 13% larger is barely over the
        line.</td></tr>
    <tr><th>62 &middot; ask</th><td>Bold, short, and not larger than body text. Usually
        a subheading. Sometimes not.</td></tr>
    <tr><th>Penalised</th><td>Anything straddling two columns: its place in the reading
        order is a guess, and reading order is what no checker ever questions.</td></tr>
  </tbody>
</table>
</div>
</section>

<section>
<h2><span class="n">06</span>Deciding what is actually a heading</h2>
<p>Size alone is a poor signal, and getting this wrong is the failure most likely to go
   unnoticed: a file where three-quarters of the content is a heading passes PDF/UA at
   106 of 106, because nothing in the standard says a heading must be one. On a real
   customer deck an early version tagged <strong>74%</strong> of all text as a heading,
   spanning H1 to H6. It is now <strong>24%</strong> on the same file, across H1 and H2.</p>
<div class="tablewrap">
<table>
  <caption>What the detection now insists on</caption>
  <thead><tr><th>Rule</th><th>Why</th></tr></thead>
  <tbody>
    <tr><th>Judged per page</th><td>One document-wide body size breaks on a file that
        mixes scales. A single dense small-print page measured the body at 10pt while
        the slides were set 14 to 20, so on those slides everything was half again the
        &ldquo;body&rdquo; size. Now the larger of the page's own dominant size and the
        document's, because too small a baseline invents headings.</td></tr>
    <tr><th>Length is a gate, not a penalty</th><td>Over 12 words, or more than one
        sentence, is a passage. Re-checked after wrapping: the role used to be decided
        from a paragraph's short first line, which then absorbed another twenty
        words.</td></tr>
    <tr><th>A figure is not a label</th><td>&ldquo;4X&rdquo;, &ldquo;$110M&rdquo;,
        &ldquo;2021&rdquo; are set large and bold exactly like headings. Requires one run
        of two or more letters, so &ldquo;QA&rdquo; still counts.</td></tr>
    <tr><th>Table cells are cells</th><td>A table already says which cells are headers
        through <code>/TH</code> and <code>/Scope</code>. One table otherwise put 34
        values, several of them single digits, into the heading list.</td></tr>
    <tr><th>Peers agree</th><td>Text set the same way on a page gets the same answer.
        Three identical cards came out as one heading and two paragraphs purely because
        the first was short enough to pass the length test.</td></tr>
    <tr><th>One H1 per page</th><td>The page's own title, found by position rather than
        by being the largest thing on it. A display word set mid-slide is often bigger
        than the title.</td></tr>
    <tr><th>A label under its figure</th><td>A heading introduces what follows it. A
        caption beneath the number it names is not a heading for the text above.</td></tr>
  </tbody>
</table>
</div>
<div class="note warn">
  <h3>Conformance cannot police this</h3>
  <p>Every one of those failures validated cleanly. The test suite asserts the
     <strong>heading share</strong> directly and the roles of purpose-built fixtures,
     and each rule was proved to bite by reintroducing the bug. Any product built on
     this needs the same, because the standard will not tell you.</p>
</div>
</section>

<section>
<h2><span class="n">07</span>Four things it refuses to fake</h2>
<p>Each of these would otherwise produce a file that looks tagged and is useless. They
   are the reason to trust the output, and they are worth protecting in any product
   version.</p>
<div class="tablewrap">
<table>
  <caption>Refusals</caption>
  <thead><tr><th>Situation</th><th>The tempting shortcut</th><th>What it does instead</th></tr></thead>
  <tbody>
    <tr><td>One reusable object drawn on several pages, each needing different
            structure</td>
        <td>Tag it once and let every page point at the same structure. The file passes
            conformance and reads the wrong content on all but one page</td>
        <td>Detects the sharing and reports it, rather than producing a file that is
            confidently wrong</td></tr>
    <tr><td>A block of aligned text that may be a table or may just be layout</td>
        <td>Tag every grid as a table. It looks thorough and lifts the tagged count</td>
        <td>Scores the evidence and asks where it is not clear. A grid whose rows cannot
            agree on a column count is not offered as a table at all</td></tr>
    <tr><td>A font the producer never embedded</td>
        <td>Say nothing and let the conformance report carry the bad news in jargon</td>
        <td>Detects it before any work is done and explains that tagging cannot fix
            it</td></tr>
    <tr><td>Text that extracts as fragments, from a variable-font export</td>
        <td>Report the conformance pass, which has no rule for this</td>
        <td>Runs its own text-integrity check and reports it alongside the verdict</td></tr>
  </tbody>
</table>
</div>
<div class="note warn">
  <h3>The standard is a floor, not a proof</h3>
  <p>PDF/UA has no rule for whether a description is useful or whether the reading order
     makes sense &mdash; the two things that most decide whether a document is actually
     usable. During development veraPDF twice reported <code>isCompliant=true</code> on
     files with real defects: one with duplicated marked-content ids, one whose entire
     text was hidden inside an artifact. Any product built on this should keep saying so
     rather than selling the number.</p>
</div>
</section>

<section>
<h2><span class="n">08</span>What has been measured</h2>
<div class="tablewrap">
<table>
  <caption>Results</caption>
  <thead><tr><th>Property</th><th>Result</th><th>How</th></tr></thead>
  <tbody>
    <tr><th>Conformance on real Slides exports</th><td>31 of 31</td>
        <td>veraPDF PDF/UA-1, 106 of 106 rules, <code>isCompliant=true</code></td></tr>
    <tr><th>Visual fidelity</th><td>261 pages</td>
        <td>Rendered before and after; pixel-identical</td></tr>
    <tr><th>Repeatability</th><td>byte-identical</td>
        <td>Tag, re-open, tag again &mdash; content streams match exactly</td></tr>
    <tr><th>Review load</th><td>9%</td>
        <td>Share of text items the app asks about, across the corpus</td></tr>
    <tr><th>Heading share</th><td>74% &rarr; 24%</td>
        <td>Proportion of text tagged as a heading on a real customer deck</td></tr>
    <tr><th>Automated checks</th><td>347</td>
        <td>Thirteen groups, exits non-zero, includes adversarial and edge cases</td></tr>
    <tr><th>Cold start</th><td>~215 ms</td>
        <td>Bundled app, from launch to a serving window</td></tr>
  </tbody>
</table>
</div>
<p>The suite deliberately asserts &ldquo;tagging introduces no new conformance
   failure&rdquo; rather than &ldquo;the output is conformant&rdquo;. Sources carry
   their own defects that no amount of tagging can fix, and the stricter-looking
   assertion fails for the wrong reasons.</p>
</section>

<section>
<h2><span class="n">09</span>Shape of the thing</h2>
<p>The engine is plain Python with two native libraries. The macOS window is a thin
   shell around the same engine, so nothing about the core is Mac-specific.</p>
<div class="tablewrap">
<table>
  <caption>Modules</caption>
  <thead><tr><th>File</th><th>Responsibility</th></tr></thead>
  <tbody>
    <tr><th><code>analyse.py</code></th><td>PDF in, proposed structure out: roles,
        reading order, list nesting, columns, tables, confidence</td></tr>
    <tr><th><code>tagger.py</code></th><td>Confirmed structure in, tagged PDF out:
        content-stream surgery and tree construction</td></tr>
    <tr><th><code>edits.py</code></th><td>Split, join and the table references they
        move &mdash; kept out of the browser because it redistributes operator
        ranges</td></tr>
    <tr><th><code>hardening.py</code></th><td>Document-level requirements: language,
        title, flags, XMP metadata</td></tr>
    <tr><th><code>checks.py</code></th><td>What still needs a human, in plain
        English</td></tr>
    <tr><th><code>server.py</code></th><td>Local HTTP API, standard library only</td></tr>
    <tr><th><code>native.py</code></th><td>The window, menus and file panels &mdash; the
        only Mac-specific file</td></tr>
    <tr><th><code>ui/index.html</code></th><td>The five-step interface, one
        self-contained file</td></tr>
  </tbody>
</table>
</div>
<p>Dependencies are <code>pikepdf</code> (qpdf) for object and stream editing,
   <code>pymupdf</code> (MuPDF) for layout and rendering, and veraPDF plus a trimmed
   Java runtime bundled for verification. A headless CLI already exists and is what the
   test suite drives, so batch or server-side operation needs no new engine work.</p>
</section>

<section>
<h2><span class="n">10</span>What productising would take</h2>
<div class="tablewrap">
<table>
  <caption>State of each part</caption>
  <thead><tr><th>Item</th><th>State</th><th>Note</th></tr></thead>
  <tbody>
    <tr><td>Core tag-tree construction</td><td><span class="chip ok">proven</span></td>
        <td>Conformant and repeatable on real files</td></tr>
    <tr><td>Guided review workflow</td><td><span class="chip ok">proven</span></td>
        <td>Confidence, reasons, per-page progress, decisions persist</td></tr>
    <tr><td>Verification in the box</td><td><span class="chip ok">proven</span></td>
        <td>veraPDF bundled; no install needed by the recipient</td></tr>
    <tr><td>Text inside form XObjects</td><td><span class="chip ok">proven</span></td>
        <td>The walker recurses into nested content streams, composing each form's
            matrix with the transform at the point it is drawn, and marked content
            inside a form is referenced through an <code>/MCR</code> against that
            stream. A form drawn on more than one page cannot carry two sets of
            structure, so that case is detected and reported rather than
            mis-tagged</td></tr>
    <tr><td>Tables as tables</td><td><span class="chip ok">proven</span></td>
        <td>Ruling-line grids become <code>/Table</code> &rsaquo; <code>/TR</code>
            &rsaquo; <code>/TH</code> or <code>/TD</code>, with <code>/Scope</code> on
            headers and spans where cells merge. Spans are measured from geometry, never
            inferred from gaps: a gap says a square is covered but not by which
            neighbour, and counting runs makes two cells claim one square. A grid whose
            rows cannot agree on a column count is not offered as a table.
            Borderless tables are not detected, by choice &mdash; the alternative
            inference cannot be separated from ordinary prose at any threshold</td></tr>
    <tr><td>Manual repair of grouping</td><td><span class="chip ok">proven</span></td>
        <td>Split, join and drag-to-reorder, for the cases geometry cannot settle.
            Splitting an item inside a table moves its pieces into the cells they were
            drawn in</td></tr>
    <tr><td>Heading quality</td><td><span class="chip ok">proven</span></td>
        <td>Seven rules beyond font size, each guarded by a test that asserts the
            heading share rather than conformance</td></tr>
    <tr><td>Windows and Linux</td><td><span class="chip todo">to build</span></td>
        <td>Engine is portable; only the window shell is Mac-specific. A browser build
            would need no new engine work</td></tr>
  </tbody>
</table>
</div>
</section>

<section>
<h2><span class="n">11</span>Questions worth settling early</h2>
<ul>
  <li><strong>Who is the user?</strong> The current design assumes the person who made
      the document, working through it once. A dedicated remediation team, or an agency
      doing hundreds, wants different affordances entirely.</li>
  <li><strong>Does unattended tagging belong in the product?</strong> Technically
      straightforward, and it forfeits the guarantee that a human saw the reading order.
      That may be an acceptable trade for bulk archives and a bad one for customer
      deliverables.</li>
  <li><strong>Where does it sit?</strong> A standalone utility, a step in an existing
      product, or an engine other tools call. The engine is already separable from the
      interface, so this is a packaging decision rather than an architectural one.</li>
  <li><strong>How much of the honesty survives commercial pressure?</strong> The refusals
      in section 7 are the reason to trust it, and they are also the features most likely
      to be argued away in favour of a clean pass rate.</li>
</ul>
</section>
"""

WORKS = page(
    title="How PDF Tagger Works",
    eyebrow="chriskeene.me &middot; engineering handover",
    h1="How PDF Tagger Works",
    lede="A working prototype that turns a PDF with no accessibility structure into a "
         "conformant tagged one, with a person confirming the parts no algorithm can "
         "settle. This is what it does, how it does it, and what turning it into a "
         "product would actually involve.",
    facts=[("Status", "Prototype, working end to end"),
           ("Platform", "macOS app; portable engine"),
           ("Evidence", "31 real decks, 347 checks")],
    body=WORKS_BODY,
    foot=f"PDF Tagger {VERSION} &middot; prototype built in Sandbox, macOS app plus "
         "portable engine. Measurements come from the project's own test suite and were "
         "reproduced against real customer-facing decks.")


if __name__ == "__main__":
    for name, html in (("installing-pdf-tagger.html", INSTALL),
                       ("how-pdf-tagger-works.html", WORKS)):
        (HERE / name).write_text(html, encoding="utf-8")
        print(f"wrote {name}  ({len(html):,} bytes)")
