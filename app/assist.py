"""Optional help from Claude: draft image descriptions, sanity-check reading order.

Off unless the user turns it on, and it never decides anything on its own. Every
draft comes back marked as a draft and still counts as outstanding until a person
has looked at it, because the one thing this app has always refused to do is
produce a description that looks finished and says nothing. PDF/UA has no rule for
whether alt text is useful, so an auto-accepted draft would sail through every
check and help nobody.

Two providers, picked at runtime:

  * the Claude Code CLI, if the user has it. No key to paste and no second bill:
    it runs on whatever account they already sign in to. This is the one most
    people will get for free.
  * the Anthropic SDK with an API key, for anyone without the CLI.

Neither is a dependency of the app. If neither resolves, the feature simply is not
offered and nothing else changes.
"""

import glob
import json
import os
import shutil
import subprocess
import tempfile

# Reading order needs the words and where they sit, never the picture, so that half
# of this sends no image data anywhere.
ORDER_MODEL = "sonnet"
ALT_MODEL = "sonnet"

_CLI_TIMEOUT = 120


class Unavailable(Exception):
    """No usable route to Claude, with a line the user can act on."""


# Where Claude Code installs itself. A .app launched from the Dock inherits the
# system PATH (/usr/bin:/bin and friends) and NOT the one from a login shell, so
# `which claude` finds nothing even on a machine that plainly has it: the native
# installer puts it under ~/.local/bin, which no GUI process has ever heard of.
# Looking in the usual places is the difference between the feature appearing and
# the app claiming Claude is not installed.
_CLI_PLACES = (
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "~/.bun/bin/claude",
    "~/.volta/bin/claude",
)


def _claude_exe():
    """The Claude Code binary, from PATH or from where it is normally installed."""
    found = shutil.which("claude")
    if found:
        return found
    for place in _CLI_PLACES:
        path = os.path.expanduser(place)
        if os.access(path, os.X_OK) and not os.path.isdir(path):
            return path
    # npm/nvm keeps one per node version, so the exact directory is not knowable.
    for path in sorted(glob.glob(os.path.expanduser(
            "~/.nvm/versions/node/*/bin/claude")), reverse=True):
        if os.access(path, os.X_OK):
            return path
    return ""


def detect():
    """Which route is available, without spending anything to find out."""
    cli = _claude_exe()
    if cli:
        return {"route": "claude-code", "detail": cli,
                "label": "Claude Code, signed in on this Mac"}
    if os.environ.get("ANTHROPIC_API_KEY"):
        return {"route": "api-key", "detail": "ANTHROPIC_API_KEY",
                "label": "Anthropic API key from the environment"}
    try:
        import anthropic                                    # noqa: F401
    except Exception:
        return {"route": None, "detail": "",
                "label": "Nothing available. Install Claude Code, or set an "
                         "ANTHROPIC_API_KEY, to switch this on."}
    return {"route": "api-key-missing", "detail": "",
            "label": "The Anthropic library is here but no key is set."}


def _run_cli(prompt, model, cwd):
    """One non-interactive Claude Code call, returning its text."""
    exe = _claude_exe()
    if not exe:
        raise Unavailable("Claude Code is not installed on this Mac.")
    try:
        env = dict(os.environ)
        env["PATH"] = os.path.dirname(exe) + os.pathsep + env.get("PATH", "")
        proc = subprocess.run(
            [exe, "-p", "--output-format", "json", "--model", model, prompt],
            cwd=cwd, capture_output=True, text=True, timeout=_CLI_TIMEOUT,
            stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        raise Unavailable("Claude Code did not answer in time.")
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        raise Unavailable("Claude Code returned something unreadable.")
    if payload.get("is_error"):
        msg = str(payload.get("result") or "").strip()
        # The common one by far, and it has a one-line fix worth naming.
        if "authenticate" in msg.lower() or "oauth" in msg.lower():
            raise Unavailable("Your Claude Code sign-in has expired. Open a "
                              "terminal, run `claude`, sign in, and try again.")
        raise Unavailable(msg or "Claude Code reported an error.")
    return str(payload.get("result") or "").strip()


def _run_api(prompt, images=()):
    """One Anthropic API call, with optional images."""
    try:
        import anthropic
    except Exception:
        raise Unavailable("The Anthropic library is not installed.")
    content = []
    for media_type, data in images:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": media_type, "data": data}})
    content.append({"type": "text", "text": prompt})
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-sonnet-5", max_tokens=1024,
            messages=[{"role": "user", "content": content}])
    except Exception as exc:                                # noqa: BLE001
        raise Unavailable(f"The Anthropic API could not be reached: {exc}")
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _ask(prompt, model, images=(), files_dir=None):
    route = detect()["route"]
    if route == "claude-code":
        return _run_cli(prompt, model, files_dir or tempfile.gettempdir())
    if route == "api-key":
        return _run_api(prompt, images)
    raise Unavailable(detect()["label"])


# --------------------------------------------------------------------------- #
# 1. Draft image descriptions
# --------------------------------------------------------------------------- #

_ALT_RULES = (
    "You are drafting alt text for one image cut out of a PDF page, for somebody "
    "reading with a screen reader.\n\n"
    "FIRST decide what the image is. Work down this list and stop at the first "
    "that fits, because the order matters:\n\n"
    "1. A LOGO or WORDMARK. A company or product name drawn as a stylised graphic "
    "is a LOGO, not text, even when the image is nothing but the name. This is the "
    "one most often got wrong: a wordmark looks like a word, so it gets "
    "transcribed as if it were a screenshot. It is a logo. Answer with the "
    "organisation's name and the word logo, for example 'Acme logo'. Leave out the "
    "styling, the strapline and any tagline sitting beside it.\n"
    "2. PURE DECORATION. A background shape, gradient, texture, divider, rule, "
    "border, bullet glyph, a photograph sitting behind a title purely for looks, "
    "or an icon that only repeats the words next to it. The test is simple: if the "
    "image vanished, would the reader lose anything at all? If not, it is "
    "decoration, and describing it is worse than skipping it because it interrupts "
    "the page to say nothing. Be willing to call this: most decks are full of it.\n"
    "3. A CHART, GRAPH or DIAGRAM. Give the kind of chart, what it measures, and "
    "the point it is making, with the figures that carry that point.\n"
    "4. A SCREENSHOT, or any image whose content is text. The words that matter "
    "have to be in the description, because they exist nowhere else.\n"
    "5. A PHOTOGRAPH or ILLUSTRATION that carries meaning. Say what is happening "
    "and who is in it, as far as it bears on the surrounding text.\n\n"
    "THEN write the description:\n"
    "- Say what a reader would miss if the image were not there.\n"
    "- One sentence, under 25 words, no trailing full stop.\n"
    "- Never open with 'image of', 'picture of', 'graphic showing' or similar: the "
    "reader is already told it is an image.\n"
    "- Describe only what you can actually see. Never infer a brand, a name, a "
    "number or a date that is not in the image.\n\n"
    "Reply in exactly these three lines and nothing else, no preamble:\n"
    "KIND: logo | decoration | chart | screenshot | photo\n"
    "ALT: the description, or the single word NONE when it is decoration\n"
    "WHY: at most twelve words on why it is that kind"
)

# The reply is meant to be three labelled lines, but a model that ignores the
# format usually still puts a usable answer on the last line, so both are read.
_KINDS = ("logo", "decoration", "chart", "screenshot", "photo")


def _parse_alt(out):
    """(text, decorative, why) from a reply, however loosely it followed the shape."""
    kind = alt = why = ""
    for line in out.splitlines():
        line = line.strip()
        low = line.lower()
        if low.startswith("kind:"):
            kind = line[5:].strip().strip(".").lower()
        elif low.startswith("alt:"):
            alt = line[4:].strip()
        elif low.startswith("why:"):
            why = line[4:].strip()

    if not kind and not alt:
        # Unlabelled reply: fall back to the old shape, last real line, and the
        # bare DECORATIVE that earlier versions of this prompt asked for.
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        alt = lines[-1] if lines else ""
        if alt.upper().startswith("DECORATIVE"):
            return "", True, ""

    alt = alt.strip().strip('"\u201c\u201d').strip()
    decorative = kind.startswith("decorat") or alt.upper() in ("NONE", "DECORATIVE")
    if decorative:
        return "", True, why
    if kind and kind not in _KINDS:
        kind = ""
    return alt, False, why


def draft_alt(crop_path, context_text="", page_no=None):
    """Draft one image description. Returns (text, decorative, why)."""
    where = f" It appears on page {page_no}." if page_no else ""
    ctx = f"\n\nText near it on the page, for context only:\n{context_text[:600]}" \
        if context_text else ""
    prompt = (f"{_ALT_RULES}\n\nThe image is the file {os.path.basename(crop_path)} "
              f"in this directory. Read it first.{where}{ctx}")
    route = detect()["route"]
    if route == "claude-code":
        out = _run_cli(prompt, ALT_MODEL, os.path.dirname(crop_path))
    else:
        import base64
        with open(crop_path, "rb") as fh:
            data = base64.standard_b64encode(fh.read()).decode("ascii")
        out = _run_api(f"{_ALT_RULES}{where}{ctx}", [("image/png", data)])
    return _parse_alt(out)


# --------------------------------------------------------------------------- #
# 2. Review the reading order
# --------------------------------------------------------------------------- #

_ORDER_RULES = (
    "Below is the reading order a tool has proposed for one page of a PDF, as it "
    "would be read aloud by a screen reader. Each line gives a position, the role, "
    "the position on the page, and the text.\n\n"
    "Find only places where the ORDER is wrong: something read before the thing "
    "it belongs to, a column read across instead of down, a caption read before "
    "its figure, a heading read after the text it introduces.\n\n"
    "Do not comment on the roles, the wording, or anything else. If the order "
    "reads sensibly, say so.\n\n"
    "Reply as JSON only, no prose, in this shape:\n"
    '{"ok": true}\n'
    "or\n"
    '{"ok": false, "problems": [{"position": 4, "why": "read before the heading '
    'it sits under"}]}\n'
)


def review_order(items, page_no):
    """Ask whether a page's reading order makes sense. Text and geometry only."""
    lines = []
    for e in items:
        x0, y0 = int(e["bbox"][0]), int(e["bbox"][1])
        text = (e.get("text") or "").replace("\n", " ")[:90]
        lines.append(f'{e["order"] + 1}. [{e["role"]}] (x{x0},y{y0}) {text}')
    prompt = f"{_ORDER_RULES}\nPage {page_no}:\n" + "\n".join(lines)
    raw = _ask(prompt, ORDER_MODEL)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        raise Unavailable("The answer could not be read as a result.")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        raise Unavailable("The answer could not be read as a result.")
    if data.get("ok"):
        return []
    out = []
    for p in data.get("problems", [])[:12]:
        try:
            pos = int(p.get("position", 0)) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= pos < len(items):
            out.append({"id": items[pos]["id"], "position": pos + 1,
                        "why": str(p.get("why", ""))[:200]})
    return out
