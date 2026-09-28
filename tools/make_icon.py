"""Generate the EpidBot 64x64 app icon (chat bubble + pulse line) as a PNG.

Pure stdlib (zlib + struct), no PIL required.
"""

import struct
import sys
import zlib

SIZE = 64

BUBBLE_BG = (255, 255, 255, 255)
PULSE = (46, 125, 50, 255)
BORDER = (27, 31, 36, 255)


def in_rounded_rect(x, y, x0, y0, x1, y1, r):
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    nx = min(x - x0, x1 - x)
    ny = min(y - y0, y1 - y)
    if nx >= r or ny >= r:
        return True
    dx = r - nx
    dy = r - ny
    return dx * dx + dy * dy <= r * r


def in_tail(x, y):
    x0, y0 = 20, 44
    x1, y1 = 34, 44
    x2, y2 = 22, 57
    d1 = (x - x1) * (y0 - y2) + (x1 - x2) * (y - y0)
    d2 = (x - x2) * (y1 - y0) + (x2 - x0) * (y - y1)
    d3 = (x - x0) * (y2 - y1) + (x0 - x1) * (y - y2)
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


def near_segment(x, y, ax, ay, bx, by, radius=2.0):
    vx, vy = bx - ax, by - ay
    wx, wy = x - ax, y - ay
    denom = vx * vx + vy * vy
    if denom == 0:
        t = 0.0
    else:
        t = max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
    px = ax + t * vx
    py = ay + t * vy
    return (x - px) ** 2 + (y - py) ** 2 <= radius * radius


PULSE_POINTS = [
    (15, 30),
    (25, 30),
    (30, 19),
    (37, 42),
    (42, 30),
    (50, 30),
]


def pixel(x, y):
    if in_rounded_rect(x, y, 7, 9, 56, 46, 8) or in_tail(x, y):
        for i in range(len(PULSE_POINTS) - 1):
            ax, ay = PULSE_POINTS[i]
            bx, by = PULSE_POINTS[i + 1]
            if near_segment(x, y, ax, ay, bx, by):
                return PULSE
        edge = (
            in_rounded_rect(x, y, 7, 9, 56, 46, 8)
            and not in_rounded_rect(x, y, 9, 11, 54, 44, 7)
        )
        if edge or (
            in_tail(x, y)
            and not (
                in_rounded_rect(x, y, 9, 11, 54, 44, 7)
                or in_tail(x + 1, y)
                and in_tail(x, y + 1)
                and in_tail(x - 1, y)
                and in_tail(x, y - 1)
            )
        ):
            return BORDER
        return BUBBLE_BG
    return (0, 0, 0, 0)


def png_chunk(tag, data):
    raw = tag + data
    return struct.pack(">I", len(data)) + raw + struct.pack(">I", zlib.crc32(raw) & 0xFFFFFFFF)


def write_png(path):
    rows = bytearray()
    for y in range(SIZE):
        rows.append(0)
        for x in range(SIZE):
            r, g, b, a = pixel(x, y)
            rows.extend((r, g, b, a))
    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += png_chunk(b"IHDR", ihdr)
    png += png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
    png += png_chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)
    print("Wrote %s (%d bytes)" % (path, len(png)))


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "com_kwarai_epidbot/icon_64x64.png"
    write_png(out)
