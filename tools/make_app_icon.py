"""Draw TuneLab's app icon from the rail's own mark.

Run:  python tools/make_app_icon.py
Writes assets/tune_lab.png (256px) and assets/tune_lab.ico (16..256), which
ui.py hands to the window, and which the PyInstaller spec and the Inno Setup
script use for the .exe and the shortcuts.

The mark is not drawn twice: it comes out of ``_icon_shapes('note')``, the same
geometry the rail's brand tile shows, so the app icon and the app's own tile
cannot drift apart. Everything is drawn 4x and downsampled, which is what keeps
the curves clean at 16px.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from PIL import Image, ImageDraw  # noqa: E402

from ui import _ICON_GRID, _icon_shapes  # noqa: E402

OUT_DIR = os.path.join(ROOT, "assets")
SS = 4                       # supersampling factor
TILE_TOP = "#4aa3f0"
TILE_BOTTOM = "#2472b8"
INK = "#ffffff"
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _extent(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _shape_points(shape):
    """The (x, y) pairs one shape draws, on the 24-grid."""
    kind, rest = shape[0], shape[1:]
    if kind in ("poly", "polyfill"):
        return list(rest[0])
    if kind in ("oval", "disc", "half"):
        cx, cy, r = rest
        return [(cx - r, cy - r), (cx + r, cy + r)]
    return [(rest[0], rest[1]), (rest[2], rest[3])]


def draw_mark(size, ink=INK):
    """The note, on its own transparent tile of *size* pixels."""
    img = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Centre the mark's own ink in the tile: the geometry is laid out for the
    # rail's 24-grid, where the note sits slightly high and left of centre.
    pts = [p for shape in _icon_shapes("note") for p in _shape_points(shape)]
    x0, y0, x1, y1 = _extent(pts)
    k = size * SS * 0.50 / (x1 - x0)
    ox = (size * SS - (x1 - x0) * k) / 2 - x0 * k
    oy = (size * SS - (y1 - y0) * k) / 2 - y0 * k

    def xy(x, y):
        return (ox + x * k, oy + y * k)

    stroke = max(2, round(size * SS * 0.072))
    for shape in _icon_shapes("note"):
        kind, rest = shape[0], shape[1:]
        if kind == "line":
            d.line([xy(rest[0], rest[1]), xy(rest[2], rest[3])],
                   fill=ink, width=stroke)
        elif kind == "poly":
            d.line([xy(x, y) for x, y in rest[0]], fill=ink, width=stroke,
                   joint="curve")
        elif kind == "disc":
            cx, cy, r = rest
            d.ellipse([xy(cx - r, cy - r), xy(cx + r, cy + r)], fill=ink)
    return img            # still supersampled, so draw_tile can compose it


def draw_tile(size):
    """The mark on the app's rounded tile, at *size* pixels."""
    tile = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    grad = Image.new("RGBA", (1, size * SS))
    gd = ImageDraw.Draw(grad)
    def rgb(col):
        return tuple(int(col[i:i + 2], 16) for i in (1, 3, 5))

    top, bottom = rgb(TILE_TOP), rgb(TILE_BOTTOM)
    for y in range(size * SS):
        t = y / max(1, size * SS - 1)
        gd.point((0, y), fill=(round(top[0] + (bottom[0] - top[0]) * t),
                               round(top[1] + (bottom[1] - top[1]) * t),
                               round(top[2] + (bottom[2] - top[2]) * t), 255))
    grad = grad.resize((size * SS, size * SS))
    mask = Image.new("L", (size * SS, size * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size * SS - 1, size * SS - 1],
        radius=round(size * SS * 0.22), fill=255)
    tile.paste(grad, (0, 0), mask)
    tile.alpha_composite(draw_mark(size))
    return tile            # supersampled; main() does the one downsample


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    png = os.path.join(OUT_DIR, "tune_lab.png")
    ico = os.path.join(OUT_DIR, "tune_lab.ico")
    master = draw_tile(256)
    final = master.resize((256, 256), Image.Resampling.LANCZOS)
    final.save(png)
    # Pillow builds the .ico by downsampling the image it is handed, so it gets
    # the supersampled master and every size comes from one render.
    master.save(ico, sizes=[(s, s) for s in ICO_SIZES])
    for path in (png, ico):
        print(f"{path}  {os.path.getsize(path)} bytes")
    print("ink bbox in the 256px png:", final.getbbox())
    print("grid:", _ICON_GRID, "shapes:", len(_icon_shapes("note")))
    # A magnified 16px and 32px render, so the sizes that actually get used in
    # a taskbar can be eyeballed next to the master.
    strip = Image.new("RGBA", (16 * 6 + 32 * 6 + 12, 32 * 6), (0, 0, 0, 0))
    strip.paste(master.resize((16, 16), Image.Resampling.LANCZOS)
                .resize((96, 96), Image.Resampling.NEAREST), (0, 0))
    strip.paste(master.resize((32, 32), Image.Resampling.LANCZOS)
                .resize((192, 192), Image.Resampling.NEAREST), (108, 0))
    preview = os.path.join(OUT_DIR, "tune_lab_sizes.png")
    strip.save(preview)
    print("16px and 32px preview ->", preview)


if __name__ == "__main__":
    main()