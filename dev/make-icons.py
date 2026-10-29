# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Draws the EmptyDiff icon (a white empty-set sign on a dark orange,
rotated rounded square) and writes the icon files to emptydiff/static/.

The shapes follow the drawing logo.svg. Run this
script after changing them: python3 dev/make-icons.py. Standard library
only; the files it writes are committed, so the server never needs it.

    favicon.svg           browsers that support SVG icons
    favicon.ico           16, 32 and 48 px (PNG data inside), also
                          fetched by browsers from /favicon.ico
    apple-touch-icon.png  180 px, for iOS ("add to home screen"): the
                          sign on a square that fills the whole icon
                          (iOS rounds the corners itself)

Keep emptydiff/templates/_brand_mark.html (the logo in the page
header) equal to the shapes below.
"""

import math
import os
import struct
import zlib

ORANGE = (212, 85, 0)  # #d45500
WHITE = (255, 255, 255)
# Everything on a 100 x 100 grid. The diamond is a square of side
# 2 * HALF with rounded corners, turned by 45 degrees around the middle.
MIDDLE = 50.0
HALF = 36.33
CORNER = 10.0
RING_RADIUS = 20.162
STROKE = 6.7
SLASH = ((30.581, 69.311), (69.714, 30.477))
STATIC = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "emptydiff", "static"
)


def in_diamond(x, y):
    """Point inside the turned rounded square."""
    dx, dy = x - MIDDLE, y - MIDDLE
    u = (dx + dy) / math.sqrt(2)
    v = (dy - dx) / math.sqrt(2)
    inner = HALF - CORNER
    qx, qy = max(abs(u) - inner, 0.0), max(abs(v) - inner, 0.0)
    return qx * qx + qy * qy <= CORNER * CORNER


def in_sign(x, y):
    """Point on the ring or on the slash (both with round ends)."""
    if abs(math.hypot(x - MIDDLE, y - MIDDLE) - RING_RADIUS) <= STROKE / 2:
        return True
    (ax, ay), (bx, by) = SLASH
    px, py = bx - ax, by - ay
    t = ((x - ax) * px + (y - ay) * py) / (px * px + py * py)
    t = min(max(t, 0.0), 1.0)
    return math.hypot(x - ax - t * px, y - ay - t * py) <= STROKE / 2


def colour(x, y, full_square):
    """RGBA of one point; the diamond only, or a full square behind."""
    if in_sign(x, y):
        return (*WHITE, 255)
    if full_square or in_diamond(x, y):
        return (*ORANGE, 255)
    return (0, 0, 0, 0)


def render(size, full_square, samples=5):
    """RGBA scan lines of the icon at `size` px, anti-aliased by
    supersampling (colours averaged with their opacity as weight)."""
    scale = 100 / (size * samples)
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            red = green = blue = alpha = 0
            for sy in range(samples):
                for sx in range(samples):
                    px = (x * samples + sx + 0.5) * scale
                    py = (y * samples + sy + 0.5) * scale
                    r, g, b, a = colour(px, py, full_square)
                    red, green, blue, alpha = (
                        red + r * a,
                        green + g * a,
                        blue + b * a,
                        alpha + a,
                    )
            if alpha:
                row += bytes(
                    round(c / alpha) for c in (red, green, blue)
                ) + bytes([round(alpha / (samples * samples))])
            else:
                row += bytes(4)
        rows.append(bytes(row))
    return rows


def png(size, full_square=False):
    def chunk(kind, data):
        body = kind + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body))
        )

    # filter type 0 per scan line
    raw = b"".join(b"\x00" + row for row in render(size, full_square))
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def ico(sizes):
    images = [png(s) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for size, data in zip(sizes, images):
        out += struct.pack(
            "<BBBBHHII", size, size, 0, 0, 1, 32, len(data), offset
        )
        offset += len(data)
    return out + b"".join(images)


def svg():
    start = MIDDLE - HALF
    (ax, ay), (bx, by) = SLASH
    orange = "#%02x%02x%02x" % ORANGE
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">\n'
        f'  <rect x="{start:g}" y="{start:g}" width="{2 * HALF:g}" '
        f'height="{2 * HALF:g}" rx="{CORNER:g}" '
        f'transform="rotate(45 {MIDDLE:g} {MIDDLE:g})" fill="{orange}"/>\n'
        f'  <circle cx="{MIDDLE:g}" cy="{MIDDLE:g}" r="{RING_RADIUS:g}" '
        f'fill="none" stroke="#fff" stroke-width="{STROKE:g}"/>\n'
        f'  <line x1="{ax:g}" y1="{ay:g}" x2="{bx:g}" y2="{by:g}" '
        f'stroke="#fff" stroke-width="{STROKE:g}" '
        'stroke-linecap="round"/>\n'
        "</svg>\n"
    )


def main():
    files = {
        "favicon.svg": svg().encode(),
        "favicon.ico": ico([16, 32, 48]),
        "apple-touch-icon.png": png(180, full_square=True),
    }
    for name, data in files.items():
        with open(os.path.join(STATIC, name), "wb") as f:
            f.write(data)
        print(f"{name}: {len(data)} bytes")


if __name__ == "__main__":
    main()
