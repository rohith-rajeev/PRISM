#!/usr/bin/env python3
"""Generate PRISM's app icon in the formats each desktop platform wants.

Stdlib only, to match the rest of the project: the PNG encoder is ~40 lines of
zlib + struct, and the ICO container is a header wrapped around those PNGs.
macOS `.icns` is produced with `iconutil`, which ships with macOS.

    python3 desktop/make_icon.py build/icons

Writes `icon.png`, `icon.ico`, and (on macOS) `icon.icns`.
"""
import struct
import subprocess
import sys
import zlib
from pathlib import Path

# Matches the in-app badge: PAL["badge_bg"] plate, PAL["badge_fg"] diamond.
PLATE = (0x1E, 0x3A, 0x5F)
GLYPH = (0x7F, 0xB0, 0xFF)
EDGE = (0x37, 0x3B, 0x43)
SS = 4  # supersampling factor; the icon is drawn big and averaged down


def _coverage(size):
    """Alpha/colour coverage for one icon face, as (rgba) rows.

    Everything is a signed-distance test evaluated on an SSxSS grid per pixel,
    which is cheaper to get right than a polygon rasteriser and gives clean
    edges on the diamond's diagonals.
    """
    n = size * SS
    half = n / 2.0
    radius = n * 0.22          # plate corner rounding
    inset = n * 0.04           # plate margin inside the canvas
    ring = n * 0.30            # diamond half-diagonal
    thick = n * 0.085          # diamond stroke width

    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            plate_hits = glyph_hits = edge_hits = 0
            for sy in range(SS):
                y = py * SS + sy + 0.5
                for sx in range(SS):
                    x = px * SS + sx + 0.5
                    # rounded square (Chebyshev distance with rounded corners)
                    dx = abs(x - half) - (half - inset - radius)
                    dy = abs(y - half) - (half - inset - radius)
                    if dx <= 0 and dy <= 0:
                        d = max(dx, dy)
                    elif dx > 0 and dy > 0:
                        d = (dx * dx + dy * dy) ** 0.5
                    else:
                        d = max(dx, dy)
                    if d <= radius:
                        plate_hits += 1
                        if d >= radius - n * 0.012:
                            edge_hits += 1
                        # diamond ring: |x|+|y| == ring
                        m = abs(x - half) + abs(y - half)
                        if ring - thick <= m <= ring:
                            glyph_hits += 1
            total = SS * SS
            a = plate_hits / total
            if a == 0:
                row += bytes((0, 0, 0, 0))
                continue
            g = glyph_hits / total
            e = edge_hits / total
            r_, g_, b_ = PLATE
            if e:
                r_ = int(r_ * (1 - e) + EDGE[0] * e)
                g_ = int(g_ * (1 - e) + EDGE[1] * e)
                b_ = int(b_ * (1 - e) + EDGE[2] * e)
            if g:
                r_ = int(r_ * (1 - g) + GLYPH[0] * g)
                g_ = int(g_ * (1 - g) + GLYPH[1] * g)
                b_ = int(b_ * (1 - g) + GLYPH[2] * g)
            row += bytes((r_, g_, b_, int(a * 255)))
        rows.append(bytes(row))
    return rows


def png_bytes(size):
    """Minimal RGBA PNG encoder (filter type 0 on every scanline)."""
    raw = b"".join(b"\x00" + r for r in _coverage(size))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def ico_bytes(sizes=(16, 32, 48, 64, 128, 256)):
    """ICO container holding PNG-compressed faces (Vista+ reads PNG in ICO)."""
    images = [(s, png_bytes(s)) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        dim = 0 if size >= 256 else size  # 256 is encoded as 0
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return header + entries + blobs


def write_icons(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    png = out / "icon.png"
    png.write_bytes(png_bytes(256))
    ico = out / "icon.ico"
    ico.write_bytes(ico_bytes())
    made = [png, ico]

    if sys.platform == "darwin":
        iconset = out / "icon.iconset"
        iconset.mkdir(exist_ok=True)
        for size in (16, 32, 128, 256, 512):
            (iconset / f"icon_{size}x{size}.png").write_bytes(png_bytes(size))
            (iconset / f"icon_{size}x{size}@2x.png").write_bytes(png_bytes(size * 2))
        icns = out / "icon.icns"
        try:
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)],
                           check=True, capture_output=True)
            made.append(icns)
        except (OSError, subprocess.CalledProcessError) as e:
            print(f"warning: iconutil failed ({e}); the .app will use the default icon")
    return made


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "build/icons"
    for p in write_icons(target):
        print(f"wrote {p} ({p.stat().st_size} bytes)")
