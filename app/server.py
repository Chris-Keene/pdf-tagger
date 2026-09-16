"""Local HTTP server behind the PDF Tagger window.

Deliberately stdlib-only: the app ships as a bundle, and every extra dependency
is more that can break on someone else's Mac.  Single user, single document, so
the state is just a module-level dict.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import socketserver
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pymupdf

from . import assist, edits
from .analyse import analyse, normalise_headings
from .checks import review, summary
from .model import ALL_ROLES, ROLE_GROUPS, DocModel
from .tagger import build

# When frozen into a .app, PyInstaller unpacks bundled data under _MEIPASS
# rather than beside this file.
_BASE = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))
UI = os.path.join(_BASE, "app", "ui")
if not os.path.isdir(UI):
    UI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
STATE = {"model": None, "source": None, "tmpdir": None, "last_out": None}
LOCK = threading.Lock()
# Set to the `native` module when running in a real window. Left None in browser
# mode, so the osascript fallbacks stay in play and nothing here imports AppKit
# unless it is actually needed.
NATIVE = None

# In a PyInstaller --windowed bundle sys.stdout is None, so a bare print() raises.
# That is worse than it sounds: the socket binds in the server constructor, so a
# print between binding and serve_forever() leaves the app listening but never
# accepting — it looks alive and every request hangs.  Everything goes through
# here instead, and nothing in here is allowed to raise.
LOGFILE = os.path.join(os.path.expanduser("~/Library/Logs"), "PDF Tagger.log")


def log(msg):
    try:
        if sys.stdout is not None:
            print(msg, flush=True)
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)
        with open(LOGFILE, "a") as f:
            f.write(str(msg).rstrip() + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def osascript(script):
    p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return p.stdout.strip(), p.returncode


def pick_file():
    # A real NSOpenPanel when we have a window to be modal to; osascript otherwise.
    if NATIVE is not None:
        return NATIVE.choose_pdf()
    out, code = osascript(
        'POSIX path of (choose file with prompt "Choose a PDF to make accessible" '
        'of type {"com.adobe.pdf"})')
    return out if code == 0 and out else None


def pick_save(default_name, default_dir):
    d = default_dir or os.path.expanduser("~/Downloads")
    if NATIVE is not None:
        return NATIVE.choose_save(default_name, d)
    out, code = osascript(
        f'POSIX path of (choose file name with prompt "Save the accessible PDF" '
        f'default name "{default_name}" default location POSIX file "{d}")')
    return out if code == 0 and out else None


def verapdf_command():
    """How to invoke veraPDF: the copy inside the app if present, else the system.

    Bundling it means a recipient gets a real conformance verdict without
    installing Java or Homebrew, which is the difference between "Exported" and
    "106 of 106 rules pass" on someone else's Mac.
    """
    roots = []
    if getattr(sys, "_MEIPASS", None):
        roots.append(sys._MEIPASS)
        # In a .app the data lands in Contents/Resources; Frameworks is a sibling.
        roots.append(os.path.join(os.path.dirname(sys._MEIPASS), "Resources"))
    roots.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    roots.append(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "build"))
    for base in roots:
        jar = os.path.join(base, "verapdf", "cli.jar")
        java = os.path.join(base, "verapdf", "jre", "bin", "java")
        if os.path.exists(jar) and os.path.exists(java):
            return [java, "-jar", jar]
    exe = shutil.which("verapdf")
    if exe:
        return [exe]
    if os.path.exists("/opt/homebrew/bin/verapdf"):
        return ["/opt/homebrew/bin/verapdf"]
    return None


def verapdf_report(path):
    cmd = verapdf_command()
    if not cmd:
        return None
    try:
        # verapdf exits non-zero on FAIL, so the return code is not an error here
        p = subprocess.run(cmd + ["-f", "ua1", "--format", "xml", path],
                           capture_output=True, text=True, timeout=600)
        import xml.etree.ElementTree as ET
        root = ET.fromstring(p.stdout)
    except Exception:
        return None
    d = root.find(".//details")
    vr = root.find(".//validationReport")
    if d is None or vr is None:
        return None
    fails = []
    for rule in root.iter("rule"):
        if rule.get("status") == "failed":
            fails.append({"clause": rule.get("clause"), "test": rule.get("testNumber"),
                          "checks": int(rule.get("failedChecks", 0)),
                          "description": (rule.findtext("description") or "").strip()})
    return {"compliant": vr.get("isCompliant") == "true",
            "passed": int(d.get("passedRules", 0)),
            "failed": int(d.get("failedRules", 0)),
            "failedChecks": int(d.get("failedChecks", 0)), "rules": fails}


def extraction_ok(path):
    """Cheap version of the text-integrity check: letters stranded alone.

    A conformant PDF can still extract as "Mo ile" / "accessi ility" when the
    source embedded a variable font, and no conformance rule catches it — so it
    is worth reporting next to the conformance verdict rather than after it.
    """
    doc = pymupdf.open(path)
    text = "\n".join(p.get_text() for p in doc)
    words = [w for w in re.split(r"\s+", text) if w]
    stranded = [w for w in words if len(w) == 1 and w.isalpha() and w.lower() not in "ai"]
    return {"words": len(words), "stranded": len(stranded),
            "ok": len(stranded) <= max(2, len(words) * 0.01)}


def friendly_open_error(exc):
    """Turn a library exception into something worth showing a person.

    pikepdf puts the full path in its messages, which leaks a temp location into
    the UI of an app that warns users about exactly that in their alt text.
    """
    import pikepdf as _pk
    if isinstance(exc, _pk.PasswordError):
        return ("That PDF is password protected, so it cannot be opened here. "
                "Remove the password and try again.")
    # pikepdf formats these as "<path>: <reason>"; drop the path half entirely
    # rather than trying to subtract known temp directories from it.
    text = str(exc)
    head, sep, tail = text.partition(": ")
    if sep and ("/" in head or head.lower().endswith(".pdf")):
        text = tail
    text = re.sub(r"(/[\w.\-]+)+", "", text).strip()
    base = "That file does not look like a readable PDF. It may be damaged, or only " \
           "partly downloaded."
    return f"{base} ({text[:110]})" if text else base


def load(path):
    with LOCK:
        model = normalise_headings(analyse(path))
        STATE["model"] = model
        STATE["source"] = path
        STATE["last_out"] = None
    return model


def payload():
    m = STATE["model"]
    if m is None:
        return {"loaded": False, "roles": ROLE_GROUPS, "native": NATIVE is not None}
    return {"loaded": True, "roles": ROLE_GROUPS, "all_roles": ALL_ROLES,
            "native": NATIVE is not None,
            "source": STATE["source"],
            "filename": os.path.basename(STATE["source"] or ""),
            "doc": m.to_json(), "summary": summary(m), "issues": review(m),
            "last_out": STATE["last_out"]}


# --------------------------------------------------------------------------- #
# request handling
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "PDFTagger/1.0"

    def log_message(self, *a):
        pass                                    # keep the console quiet

    # -- plumbing ----------------------------------------------------------- #
    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # -- GET ---------------------------------------------------------------- #
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._file(os.path.join(UI, "index.html"), "text/html; charset=utf-8")
        if u.path == "/api/state":
            return self._json(payload())
        if u.path == "/api/page":
            return self._page(q)
        if u.path == "/api/crop":
            return self._crop(q)
        return self._send(404, b"not found", "text/plain")

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                return self._send(200, f.read(), ctype)
        except OSError:
            return self._send(404, b"not found", "text/plain")

    def _page(self, q):
        if STATE["source"] is None:
            return self._send(404, b"no document", "text/plain")
        n = int(q.get("n", ["0"])[0])
        # Ceiling raised for the zoom control: at 4x on a wide panel the fit-width
        # render would be visibly soft, and softness defeats the point of zooming
        # in to check which glyphs a box actually covers.
        w = min(int(q.get("w", ["1400"])[0]), 4400)
        doc = pymupdf.open(STATE["source"])
        if not 0 <= n < len(doc):
            return self._send(404, b"no such page", "text/plain")
        page = doc[n]
        zoom = w / page.rect.width
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return self._send(200, pix.tobytes("png"), "image/png")

    def _crop(self, q):
        """A tight render of one element, so figures can be judged in place."""
        if STATE["source"] is None:
            return self._send(404, b"no document", "text/plain")
        n = int(q.get("n", ["0"])[0])
        box = [float(v) for v in q.get("box", ["0,0,10,10"])[0].split(",")]
        doc = pymupdf.open(STATE["source"])
        page = doc[n]
        pad = 4
        rect = pymupdf.Rect(box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)
        rect = rect & page.rect
        if rect.is_empty:
            rect = page.rect
        zoom = min(6.0, max(1.0, 420 / max(rect.width, 1)))
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=rect, alpha=False)
        return self._send(200, pix.tobytes("png"), "image/png")

    # -- POST --------------------------------------------------------------- #
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/api/pick":
                p = pick_file()
                if not p:
                    return self._json({"cancelled": True})
                load(p)
                return self._json(payload())

            if u.path == "/api/upload":
                name = self.headers.get("X-Filename") or "document.pdf"
                data = self._body()
                if not data:
                    return self._json({"error": "No file received."}, 400)
                if STATE["tmpdir"] is None:
                    STATE["tmpdir"] = tempfile.mkdtemp(prefix="pdftagger-")
                safe = re.sub(r"[^\w.\- ]", "_", os.path.basename(name))
                path = os.path.join(STATE["tmpdir"], safe)
                with open(path, "wb") as f:
                    f.write(data)
                try:
                    load(path)
                except Exception as exc:
                    return self._json({"error": friendly_open_error(exc)}, 400)
                return self._json(payload())

            if u.path == "/api/model":
                data = json.loads(self._body() or b"{}")
                with LOCK:
                    STATE["model"] = DocModel.from_json(data["doc"])
                return self._json(payload())

            if u.path == "/api/edit":
                data = json.loads(self._body() or b"{}")
                with LOCK:
                    if data.get("doc"):
                        STATE["model"] = DocModel.from_json(data["doc"])
                    if STATE["model"] is None:
                        return self._json({"error": "Nothing is open."}, 400)
                    act = data.get("action")
                    if act == "split":
                        ok, info = edits.split(STATE["model"], data.get("id"))
                    elif act == "join":
                        ok, info = edits.join_up(STATE["model"], data.get("id"))
                    else:
                        return self._json({"error": f"Unknown edit {act!r}."}, 400)
                out = payload()
                out["ok"] = ok
                out["select" if ok else "message"] = info
                return self._json(out)

            if u.path == "/api/assist":
                data = json.loads(self._body() or b"{}")
                act = data.get("action")
                if act == "detect":
                    return self._json({"assist": assist.detect()})
                with LOCK:
                    if data.get("doc"):
                        STATE["model"] = DocModel.from_json(data["doc"])
                    model = STATE["model"]
                if model is None:
                    return self._json({"error": "Nothing is open."}, 400)
                try:
                    if act == "alt":
                        n = _draft_alt_text(model, data.get("ids") or [])
                        out = payload()
                        out["drafted"] = n
                        return self._json(out)
                    if act == "order":
                        return self._json(
                            {"page": data.get("page", 0),
                             "problems": _review_order(model, data.get("page", 0))})
                except assist.Unavailable as exc:
                    return self._json({"error": str(exc)}, 503)
                return self._json({"error": f"Unknown action {act!r}."}, 400)

            if u.path == "/api/export":
                return self._export(json.loads(self._body() or b"{}"))

            if u.path == "/api/reveal":
                data = json.loads(self._body() or b"{}")
                p = data.get("path") or STATE["last_out"]
                if p and os.path.exists(p):
                    subprocess.run(["open", "-R", p])
                return self._json({"ok": True})

            if u.path == "/api/quit":
                self._json({"ok": True})
                if NATIVE is not None:
                    threading.Thread(target=NATIVE.terminate, daemon=True).start()
                else:
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                return

            return self._send(404, b"not found", "text/plain")
        except Exception as exc:                        # never leave the UI hanging
            import traceback
            log(traceback.format_exc())
            return self._json({"error": str(exc)}, 500)

    def _export(self, data):
        if STATE["model"] is None:
            return self._json({"error": "Nothing is open."}, 400)
        with LOCK:
            if data.get("doc"):
                STATE["model"] = DocModel.from_json(data["doc"])
            model = STATE["model"]
        src = STATE["source"]
        stem = os.path.splitext(os.path.basename(src))[0]
        default_name = f"{stem}-accessible.pdf"
        out = data.get("out")
        if data.get("ask"):
            base = os.path.dirname(src)
            if STATE["tmpdir"] and base == STATE["tmpdir"]:
                base = os.path.expanduser("~/Downloads")
            out = pick_save(default_name, base)
            if not out:
                return self._json({"cancelled": True})
        if not out:
            base = os.path.dirname(src)
            if STATE["tmpdir"] and base == STATE["tmpdir"]:
                base = os.path.expanduser("~/Downloads")
            out = os.path.join(base, default_name)
        if not out.lower().endswith(".pdf"):
            out += ".pdf"
        build(model, out)
        STATE["last_out"] = out
        return self._json({"out": out, "verapdf": verapdf_report(out),
                           "extraction": extraction_ok(out),
                           "summary": summary(model)})


def _draft_alt_text(model, ids):
    """Draft a description for each named image, from a crop of just that image.

    Only the image itself is sent, never the whole page: a crop is the least that
    can answer the question, and on a customer deck the rest of the slide is very
    often the part nobody wants leaving the machine.
    """
    wanted = set(ids)
    doc = pymupdf.open(model.path)
    drafted = 0
    with tempfile.TemporaryDirectory(prefix="pdftagger-alt-") as tmp:
        for pm in model.pages:
            page = doc[pm.number]
            context = " ".join(
                (e.text or "") for e in sorted(pm.elements, key=lambda x: x.order)
                if e.kind == "text" and e.role != "Artifact")[:600]
            for e in pm.elements:
                if e.kind != "image" or (wanted and e.id not in wanted):
                    continue
                if e.role == "Artifact" or (e.alt or "").strip():
                    continue
                x0, y0, x1, y1 = e.bbox
                if x1 - x0 < 4 or y1 - y0 < 4:
                    continue
                crop = os.path.join(tmp, f"{e.id}.png")
                page.get_pixmap(clip=pymupdf.Rect(x0, y0, x1, y1),
                                dpi=150).save(crop)
                text, decorative = assist.draft_alt(crop, context, pm.number + 1)
                if decorative:
                    e.role = e.auto_role = "Artifact"
                    e.note = ("Claude read this as decoration. Nothing has been "
                              "settled: check it before you export.")
                elif text:
                    e.alt = text
                    e.note = ("Drafted by Claude from the image itself. Read it "
                              "against the picture before you accept it.")
                else:
                    continue
                # A draft is not a decision. It stays outstanding until a person
                # has looked at it, which is the whole point of offering one.
                e.reviewed = False
                e.score, e.confidence = 50, "low"
                e.drafted = True
                drafted += 1
    return drafted


def _review_order(model, page_no):
    pm = model.pages[max(0, min(len(model.pages) - 1, page_no))]
    items = [{"id": e.id, "order": e.order, "role": e.role,
              "bbox": list(e.bbox), "text": e.text}
             for e in sorted(pm.elements, key=lambda x: x.order)
             if e.role != "Artifact" and e.kind == "text"]
    if len(items) < 3:
        return []
    return assist.review_order(items, pm.number + 1)


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse DNS lookup.

    `HTTPServer.server_bind` calls `socket.getfqdn()` purely to fill in
    `server_name`, which nothing here reads.  Inside the frozen bundle that
    lookup blocks for about 35 seconds, and because it runs *after* the socket
    is bound the app sits there listening but never accepting: it looks alive
    and every request hangs until the lookup gives up.
    """

    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def serve(port=0, open_browser=True, native=False):
    global NATIVE
    try:
        httpd = Server(("127.0.0.1", port), Handler)
    except OSError:
        httpd = Server(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/"

    if native:
        try:
            from . import native as native_mod
        except Exception as exc:                      # noqa: BLE001
            log(f"native window unavailable ({exc}); falling back to the browser")
            native = False
        else:
            NATIVE = native_mod

    log(f"PDF Tagger running at {url} ({'native window' if native else 'browser'})")
    try:
        if native:
            # Cocoa insists on owning the main thread, so the server moves to a
            # daemon thread and the window drives the process from here.
            threading.Thread(target=httpd.serve_forever, daemon=True).start()

            def opened(path):
                try:
                    load(path)
                    NATIVE.reload_window()
                except Exception as exc:              # noqa: BLE001
                    log(f"could not open {path}: {exc}")

            NATIVE.run_window(url, on_open=opened)
        else:
            if open_browser:
                subprocess.run(["open", url])
            httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
        if STATE["tmpdir"]:
            shutil.rmtree(STATE["tmpdir"], ignore_errors=True)


if __name__ == "__main__":
    serve(port=int(sys.argv[1]) if len(sys.argv) > 1 else 0)
