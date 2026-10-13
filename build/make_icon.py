"""Draw the app icon with pymupdf, so the build needs no extra dependencies."""
import math
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


def _clip(p, d, r):
    """The part of the line through p with direction d that lies inside rect r, or None."""
    t0, t1 = -1e9, 1e9
    for pk, qk in ((-d[0], p[0] - r.x0), (d[0], r.x1 - p[0]), (-d[1], p[1] - r.y0), (d[1], r.y1 - p[1])):
        if pk == 0:
            if qk < 0:
                return None
            continue
        t = qk / pk
        if pk < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
    if t0 >= t1:
        return None
    return (pymupdf.Point(p[0] + t0 * d[0], p[1] + t0 * d[1]),
            pymupdf.Point(p[0] + t1 * d[0], p[1] + t1 * d[1]))


def _bez(p, t):
    """Point and unit tangent at t on the cubic bezier p[0..3]."""
    u = 1 - t
    pt = [u ** 3 * p[0][i] + 3 * u * u * t * p[1][i] + 3 * u * t * t * p[2][i] + t ** 3 * p[3][i] for i in (0, 1)]
    dv = [3 * u * u * (p[1][i] - p[0][i]) + 6 * u * t * (p[2][i] - p[1][i]) + 3 * t * t * (p[3][i] - p[2][i])
          for i in (0, 1)]
    n = math.hypot(*dv) or 1
    return pt, (dv[0] / n, dv[1] / n)


def _leaf(shape, base, angle, length, half_width):
    """An almond leaf from base towards angle (radians)."""
    ca, sa = math.cos(angle), math.sin(angle)
    tip = pymupdf.Point(base[0] + length * ca, base[1] + length * sa)
    mid = (base[0] + length * 0.45 * ca, base[1] + length * 0.45 * sa)
    nx, ny = -sa * half_width, ca * half_width
    b = pymupdf.Point(*base)
    shape.draw_bezier(b, pymupdf.Point(mid[0] + nx, mid[1] + ny), pymupdf.Point(mid[0] + nx * 0.6 + (tip.x - mid[0]) * 0.5, mid[1] + ny * 0.6 + (tip.y - mid[1]) * 0.5), tip)
    shape.draw_bezier(tip, pymupdf.Point(mid[0] - nx * 0.6 + (tip.x - mid[0]) * 0.5, mid[1] - ny * 0.6 + (tip.y - mid[1]) * 0.5), pymupdf.Point(mid[0] - nx, mid[1] - ny), b)


def draw(size):
    doc = pymupdf.open()
    page = doc.new_page(width=size, height=size)
    s = size / 1024.0
    shape = page.new_shape()
    # rounded ink plate
    shape.draw_rect(pymupdf.Rect(0, 0, size, size), radius=0.22)
    shape.finish(color=None, fill=INK)
    # a page with a folded corner: it is still a PDF tool
    pw, ph = 540 * s, 680 * s
    px, py = (size - pw) / 2, (size - ph) / 2
    fold = 150 * s
    shape.draw_polyline([
        pymupdf.Point(px, py), pymupdf.Point(px + pw - fold, py),
        pymupdf.Point(px + pw, py + fold), pymupdf.Point(px + pw, py + ph),
        pymupdf.Point(px, py + ph), pymupdf.Point(px, py)])
    shape.finish(color=None, fill=WHITE, closePath=True)
    # the trellis: a framed panel of diamonds, the structure the app builds into the page.
    # Few, thick slats so the criss-cross still reads at 16px; the frame hides the cut ends.
    lat = pymupdf.Rect(px + 85 * s, py + 215 * s, px + pw - 85 * s, py + ph - 85 * s)
    gap = lat.width / 2
    for d in ((1, 1), (1, -1)):
        for k in range(-6, 7):
            start = (lat.x0 + k * gap, lat.y0 if d[1] > 0 else lat.y1)
            seg = _clip(start, d, lat)
            if seg and seg[0].distance_to(seg[1]) > 60 * s:
                shape.draw_line(seg[0], seg[1])
    shape.finish(color=ACCENT_INK, width=22 * s, lineCap=0, closePath=False)
    shape.draw_rect(lat, radius=0.06)
    shape.finish(color=ACCENT_INK, width=30 * s, fill=None)
    # a vine climbing it, in the bright accent: what a trellis is for
    vine = [(lat.x0 + 40 * s, lat.y1 + 5 * s), (lat.x0 + 300 * s, lat.y1 - 120 * s),
            (lat.x0 + 60 * s, lat.y0 + 170 * s), (lat.x1 - 120 * s, lat.y0 - 40 * s)]
    shape.draw_bezier(*[pymupdf.Point(*v) for v in vine])
    # closePath=False: finish() closes paths by default, which drew a chord from the
    # vine's tip straight back to its root.
    shape.finish(color=ACCENT, width=24 * s, lineCap=1, closePath=False)
    for t, side in ((0.3, 1), (0.56, -1), (0.8, 1), (1.0, -1)):
        pt, tan = _bez(vine, t)
        ang = math.atan2(tan[1], tan[0]) + side * math.radians(50)
        _leaf(shape, pt, ang, 115 * s, 38 * s)
    shape.finish(color=None, fill=ACCENT, closePath=True)
    shape.commit()
    return page.get_pixmap(alpha=True)


def main():
    iconset = os.path.join(OUT, "icon", "Trellis.iconset")
    os.makedirs(iconset, exist_ok=True)
    for base in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = base * scale
            name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
            draw(px).save(os.path.join(iconset, name))
    icns = os.path.join(OUT, "icon", "Trellis.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
    draw(512).save(os.path.join(OUT, "icon", "preview.png"))
    print("wrote", icns)


if __name__ == "__main__":
    main()
