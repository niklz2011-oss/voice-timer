"""Генерирует PNG-иконки PWA (без внешних зависимостей).

Запуск: ./sandbox python tools/make_icons.py
Рисует тот же значок, что и src/icons/icon.svg (координаты в системе 512x512).
"""
import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "icons"

BG = (0x1E, 0x29, 0x3B)
GREEN = (0x22, 0xC5, 0x5E)
WHITE = (0xF1, 0xF5, 0xF9)
SS = 3  # суперсэмплинг для сглаживания


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def in_round_rect(x, y, x0, y0, w, h, r):
    if not (x0 <= x <= x0 + w and y0 <= y <= y0 + h):
        return False
    cx = min(max(x, x0 + r), x0 + w - r)
    cy = min(max(y, y0 + r), y0 + h - r)
    return math.hypot(x - cx, y - cy) <= r


def color_at(x, y, full_bleed, scale):
    """x, y — в координатах 512. scale < 1 сжимает рисунок к центру (для maskable)."""
    if full_bleed:
        bg_hit = True
    else:
        bg_hit = in_round_rect(x, y, 0, 0, 512, 512, 96)
    if not bg_hit:
        return None
    # перевод в координаты рисунка
    gx = 256 + (x - 256) / scale
    gy = 256 + (y - 256) / scale
    if math.hypot(gx - 256, gy - 280) <= 18:
        return WHITE
    if seg_dist(gx, gy, 256, 280, 256, 180) <= 14 or seg_dist(gx, gy, 256, 280, 326, 320) <= 14:
        return WHITE
    if abs(math.hypot(gx - 256, gy - 280) - 160) <= 16:
        return GREEN
    if in_round_rect(gx, gy, 226, 72, 60, 40, 10):
        return GREEN
    return BG


def render(size, full_bleed=False, scale=1.0):
    rows = []
    k = 512 / size
    for py in range(size):
        row = bytearray([0])  # filter type None
        for px in range(size):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    x = (px + (sx + 0.5) / SS) * k
                    y = (py + (sy + 0.5) / SS) * k
                    c = color_at(x, y, full_bleed, scale)
                    if c:
                        r += c[0]; g += c[1]; b += c[2]; a += 255
            n = SS * SS
            cov = a / 255
            if cov:
                row += bytes((round(r / cov), round(g / cov), round(b / cov), round(a / n)))
            else:
                row += b"\0\0\0\0"
        rows.append(bytes(row))
    return rows


def write_png(path, size, rows):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(rows)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)
    print(f"{path.relative_to(OUT.parent.parent)}  {size}x{size}  {len(png)} B")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    write_png(OUT / "icon-192.png", 192, render(192))
    write_png(OUT / "icon-512.png", 512, render(512))
    # maskable: фон на весь квадрат, рисунок в безопасной зоне (~80%)
    write_png(OUT / "icon-maskable-512.png", 512, render(512, full_bleed=True, scale=0.75))
    # iOS сама скругляет углы — фон на весь квадрат, без прозрачности
    write_png(OUT / "apple-touch-icon.png", 180, render(180, full_bleed=True, scale=0.9))


if __name__ == "__main__":
    main()
