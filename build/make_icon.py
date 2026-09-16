"""Draw the app icon with pymupdf, so the build needs no extra dependencies."""
import os, subprocess, sys
import pymupdf

def _rgb(hexcode):
    h = hexcode.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


# chriskeene.me: deep sky blue on near-black ink, with the darker blue for the
# lines that are only meant to read as texture at 16px.
INK = _rgb("#0B1F2A")
ACCENT = _rgb("#00BFFF")
ACCENT_INK = _rgb("#006699")
WHITE = (1, 1, 1)
OUT = os.path.dirname(os.path.abspath(__file__))


def draw(size):
    doc = pymupdf.open()
    page = doc.new_page(width=size, height=size)
    s = size / 1024.0
    shape = page.new_shape()
    # rounded ink plate
    r = pymupdf.Rect(0, 0, size, size)
    shape.draw_rect(r, radius=0.22)
    shape.finish(color=None, fill=INK)
    # a page with a folded corner
    pw, ph = 470 * s, 610 * s
    px, py = (size - pw) / 2 - 40 * s, (size - ph) / 2
    fold = 130 * s
    shape.draw_polyline([
        pymupdf.Point(px, py), pymupdf.Point(px + pw - fold, py),
        pymupdf.Point(px + pw, py + fold), pymupdf.Point(px + pw, py + ph),
        pymupdf.Point(px, py + ph), pymupdf.Point(px, py)])
    shape.finish(color=None, fill=WHITE, closePath=True)
    # tag lines on the page, the thing the app actually adds
    for i, (w, col) in enumerate([(0.62, ACCENT), (0.82, ACCENT_INK),
                                 (0.82, ACCENT_INK), (0.5, ACCENT_INK)]):
        ly = py + (200 + i * 92) * s
        lr = pymupdf.Rect(px + 62 * s, ly, px + 62 * s + (pw - 124 * s) * w, ly + 42 * s)
        shape.draw_rect(lr, radius=0.45)
        shape.finish(color=None, fill=col)
    # the tag itself, hanging off the corner
    tw, th = 300 * s, 190 * s
    tx, ty = px + pw - 80 * s, py + ph - th - 60 * s
    shape.draw_polyline([
        pymupdf.Point(tx, ty + th / 2), pymupdf.Point(tx + 92 * s, ty),
        pymupdf.Point(tx + tw, ty), pymupdf.Point(tx + tw, ty + th),
        pymupdf.Point(tx + 92 * s, ty + th), pymupdf.Point(tx, ty + th / 2)])
    shape.finish(color=None, fill=ACCENT, closePath=True)
    shape.draw_circle(pymupdf.Point(tx + 138 * s, ty + th / 2), 26 * s)
    shape.finish(color=None, fill=WHITE)
    shape.commit()
    pix = page.get_pixmap(alpha=True)
    return pix


def main():
    iconset = os.path.join(OUT, "icon", "PDFTagger.iconset")
    os.makedirs(iconset, exist_ok=True)
    for base in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = base * scale
            name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
            draw(px).save(os.path.join(iconset, name))
    icns = os.path.join(OUT, "icon", "PDFTagger.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
    draw(512).save(os.path.join(OUT, "icon", "preview.png"))
    print("wrote", icns)


if __name__ == "__main__":
    main()
