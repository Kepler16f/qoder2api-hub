#!/usr/bin/env python3
"""make_icons.py —— 生成桌面客户端图标资产（纯标准库，零第三方依赖）。

产出（desktop/assets/）：
    qoder2api.png   1024x1024 母图（tkinter iconphoto / Linux 用）
    qoder2api.ico   多尺寸 PNG 压缩条目（256/128/64/48/32/16，Windows 用）
    qoder2api.icns  ic07/ic08/ic09/ic10/ic11/ic12（macOS 用）

图形：蓝绿渐变圆角方块 + 白色对话气泡 + 气泡内渐变双箭头「»」（转发/代理隐喻）。
绘制用 2x 超采样（2048 网格逐像素 SDF 判定）抗锯齿，再盒式降采样到 1024。

    python desktop/make_icons.py
"""
import os
import struct
import zlib

S = 1024            # 母图边长
SS = 2              # 超采样倍数
MARGIN = 64         # 圆角方块外边距
RADIUS = 224        # 圆角半径
COLOR_TOP = (30, 111, 235)     # #1E6FEB
COLOR_BOT = (13, 189, 139)     # #0DBD8B
WHITE = (255, 255, 255)

# 气泡与箭头几何（1024 坐标系）
BUBBLE = (272, 296, 752, 600)          # x0, y0, x1, y1
BUBBLE_R = 120
TAIL = ((352, 566), (352, 744), (548, 566))   # 左下尾巴三角
CHEVRON_T = 58                          # 箭头线宽
CHEVRONS = (                            # 每个箭头 = 折线顶点序列
    ((430, 368), (548, 448), (430, 528)),
    ((582, 368), (700, 448), (582, 528)),
)


# ---------------------------------------------------------------------------
# SDF / 几何判定
# ---------------------------------------------------------------------------
def sd_round_rect(px, py, box, r):
    x0, y0, x1, y1 = box
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    hx = (x1 - x0) / 2.0 - r
    hy = (y1 - y0) / 2.0 - r
    dx = abs(px - cx) - hx
    dy = abs(py - cy) - hy
    ax = max(dx, 0.0)
    ay = max(dy, 0.0)
    return (ax * ax + ay * ay) ** 0.5 + min(max(dx, dy), 0.0) - r


def sd_segment(px, py, a, b):
    ax, ay = a
    bx, by = b
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    denom = vx * vx + vy * vy
    t = 0.0 if denom == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
    dx = px - (ax + t * vx)
    dy = py - (ay + t * vy)
    return (dx * dx + dy * dy) ** 0.5


def inside_triangle(px, py, tri):
    (x1, y1), (x2, y2), (x3, y3) = tri
    d1 = (px - x2) * (y1 - y2) - (x1 - x2) * (py - y2)
    d2 = (px - x3) * (y2 - y3) - (x2 - x3) * (py - y3)
    d3 = (px - x1) * (y3 - y1) - (x3 - x1) * (py - y1)
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def lerp_color(t):
    return tuple(int(a + (b - a) * t) for a, b in zip(COLOR_TOP, COLOR_BOT))


def sample(nx, ny):
    """nx, ny ∈ [0, S)；返回 (r, g, b, a)，形状边缘由逐像素判定（配合超采样）。"""
    # 背景圆角方块 + 垂直渐变
    if sd_round_rect(nx, ny, (MARGIN, MARGIN, S - MARGIN, S - MARGIN),
                     RADIUS) > 0:
        return (0, 0, 0, 0)
    col = lerp_color(ny / float(S))
    # 白色对话气泡（圆角矩形 + 尾巴）
    in_bubble = sd_round_rect(nx, ny, BUBBLE, BUBBLE_R) <= 0 \
        or inside_triangle(nx, ny, TAIL)
    if in_bubble:
        # 气泡内的双箭头用渐变色，其余白底
        for pts in CHEVRONS:
            for a, b in zip(pts, pts[1:]):
                if sd_segment(nx, ny, a, b) <= CHEVRON_T / 2.0:
                    return col + (255,)
        return WHITE + (255,)
    return col + (255,)


def render_master():
    n = S * SS
    acc = [[ [0, 0, 0, 0] for _ in range(S)] for _ in range(S)]
    step = 1.0 / SS
    for iy in range(n):
        ny = (iy + 0.5) * step
        row_base = acc[iy // SS]
        sy = iy % SS
        for ix in range(n):
            r, g, b, a = sample((ix + 0.5) * step, ny)
            if a:
                cell = row_base[ix // SS]
                cell[0] += r; cell[1] += g; cell[2] += b; cell[3] += a
    total = SS * SS
    rows = []
    for row in acc:
        line = bytearray()
        for cell in row:
            line += bytes(v // total for v in cell)
        rows.append(line)
    return rows


def resize(rows, sw, sh, dw, dh):
    """盒式面积平均缩放（rows: list[bytearray] RGBA）。"""
    out = []
    for oy in range(dh):
        sy0 = oy * sh / dh
        sy1 = (oy + 1) * sh / dh
        line = bytearray(dw * 4)
        for ox in range(dw):
            sx0 = ox * sw / dw
            sx1 = (ox + 1) * sw / dw
            rs = gs = bs = as_ = cnt = 0
            for sy in range(int(sy0), max(int(sy0) + 1, int(sy1 + 0.999))):
                row = rows[min(sy, sh - 1)]
                for sx in range(int(sx0), max(int(sx0) + 1, int(sx1 + 0.999))):
                    o = min(sx, sw - 1) * 4
                    rs += row[o]; gs += row[o + 1]
                    bs += row[o + 2]; as_ += row[o + 3]
                    cnt += 1
            base = ox * 4
            line[base] = rs // cnt
            line[base + 1] = gs // cnt
            line[base + 2] = bs // cnt
            line[base + 3] = as_ // cnt
        out.append(line)
    return out


# ---------------------------------------------------------------------------
# 容器格式
# ---------------------------------------------------------------------------
def png_chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def encode_png(rows, w, h):
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", ihdr)
            + png_chunk(b"IDAT", zlib.compress(raw, 9)) + png_chunk(b"IEND", b""))


def encode_ico(pngs):
    """pngs: [(size, png_bytes)]；Vista+ 支持 PNG 压缩条目。"""
    out = [struct.pack("<HHH", 0, 1, len(pngs))]
    offset = 6 + 16 * len(pngs)
    for size, data in pngs:
        out.append(struct.pack("<BBBBHHII",
                               size % 256, size % 256, 0, 0, 1, 32,
                               len(data), offset))
        offset += len(data)
    out.extend(data for _, data in pngs)
    return b"".join(out)


def encode_icns(entries):
    """entries: [(fourcc, png_bytes)]，ICNS 长度用大端。"""
    body = b"".join(ctype.encode("ascii") + struct.pack(">I", len(data) + 8) + data
                    for ctype, data in entries)
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    assets = os.path.join(here, "assets")
    os.makedirs(assets, exist_ok=True)

    print("rendering %dx%d master (SS=%d) ..." % (S, S, SS))
    rows = render_master()
    png_path = os.path.join(assets, "qoder2api.png")
    with open(png_path, "wb") as fh:
        fh.write(encode_png(rows, S, S))
    print("  wrote", png_path)

    def png_at(size):
        if size == S:
            return encode_png(rows, S, S)
        small = resize(rows, S, S, size, size)
        return encode_png(small, size, size)

    print("building .ico ...")
    ico = encode_ico([(s, png_at(s)) for s in (256, 128, 64, 48, 32, 16)])
    ico_path = os.path.join(assets, "qoder2api.ico")
    with open(ico_path, "wb") as fh:
        fh.write(ico)
    print("  wrote", ico_path)

    print("building .icns ...")
    codes = [("ic10", 1024), ("ic09", 512), ("ic08", 256), ("ic07", 128),
             ("ic12", 64), ("ic11", 32)]
    icns = encode_icns([(cc, png_at(sz)) for cc, sz in codes])
    icns_path = os.path.join(assets, "qoder2api.icns")
    with open(icns_path, "wb") as fh:
        fh.write(icns)
    print("  wrote", icns_path)


if __name__ == "__main__":
    main()
