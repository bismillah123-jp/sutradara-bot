"""Komposer sheet storyboard (PIL) - format: Marketing Angle + info + scene."""
from PIL import Image, ImageDraw, ImageFont

RED = (200, 16, 46)
DARK = (26, 26, 26)
GRAY = (70, 70, 70)
GREEN = (22, 163, 74)
PANEL = (247, 245, 240)
WHITE = (255, 255, 255)
FONT_DIR = "/usr/share/fonts/truetype/dejavu"


def _font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(f"{FONT_DIR}/{name}", size)
    except Exception:
        return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: int):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = f"{cur} {w}".strip()
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _paste_cover(canvas, path: str, box):
    x0, y0, x1, y1 = box
    tw, th = x1 - x0, y1 - y0
    try:
        im = Image.open(path).convert("RGB")
    except Exception:
        im = Image.new("RGB", (tw, th), (220, 220, 220))
    scale = max(tw / im.width, th / im.height)
    nw, nh = int(im.width * scale + 0.5), int(im.height * scale + 0.5)
    im = im.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - tw) // 2, (nh - th) // 2
    im = im.crop((left, top, left + tw, top + th))
    canvas.paste(im, (x0, y0))


def compose_sheet(board: dict, produk: str, jenis: str, durasi: int,
                  hero_path: str, frame_paths: list, out_path: str):
    scenes = board["scenes"]
    n = len(scenes)
    W = 1800
    M = 48
    tmp = Image.new("RGB", (10, 10))
    d = ImageDraw.Draw(tmp)
    f_title = _font(46, True)
    f_tag = _font(56, True)
    f_bullet = _font(30)
    f_label = _font(25, True)
    f_value = _font(28)
    f_scene_title = _font(40, True)
    f_cap_label = _font(24, True)
    f_cap = _font(26)

    left_w = 990
    tag_lines = _wrap(d, "\u201c" + board.get("marketing_angle", "") + "\u201d", f_tag, left_w)
    bullet_total = 0
    for sp in board.get("selling_points", [])[:4]:
        bullet_total += len(_wrap(d, sp, f_bullet, left_w - 44))
    header_h = 70 + len(tag_lines) * 68 + 26 + bullet_total * 40 + 30
    header_h = max(header_h, 430)

    gap = 16
    col_w = (W - 2 * M - (n - 1) * gap) // n
    frame_h = int(col_w * 1.22)
    cap_h = 430
    title_h = 84
    H = 40 + header_h + 24 + title_h + frame_h + cap_h + 50

    img = Image.new("RGB", (W, H), WHITE)
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle([M - 12, 28, W - M + 12, 28 + header_h], radius=18, fill=PANEL)

    x, y = M + 12, 52
    dr.text((x, y), "MARKETING ANGLE", font=f_title, fill=RED)
    y += 66
    for ln in tag_lines:
        dr.text((x, y), ln, font=f_tag, fill=DARK)
        y += 68
    y += 18
    for sp in board.get("selling_points", [])[:4]:
        lines = _wrap(dr, sp, f_bullet, left_w - 44)
        for j, ln in enumerate(lines):
            if j == 0:
                dr.text((x, y), "\u2713", font=f_bullet, fill=GREEN)
            dr.text((x + 44, y), ln, font=f_bullet, fill=GRAY)
            y += 40

    hero_x0 = M + left_w + 40
    _paste_cover(img, hero_path, (hero_x0, 52, hero_x0 + 320, 52 + header_h - 60))

    ix = hero_x0 + 348
    iy = 56
    infos = [
        ("PRODUK:", produk),
        ("JENIS VIDEO:", jenis),
        ("DURASI:", f"{durasi} Detik ({n} Scene)"),
        ("TARGET AUDIENCE:", board.get("target_audience", "-")),
        ("OBJECTIVE:", board.get("objective", "-")),
    ]
    for label, value in infos:
        dr.text((ix, iy), label, font=f_label, fill=RED)
        iy += 34
        for ln in _wrap(dr, value, f_value, W - M - ix)[:3]:
            dr.text((ix, iy), ln, font=f_value, fill=DARK)
            iy += 36
        iy += 16

    sy = 28 + header_h + 24
    dr.text((M, sy), f"STORYBOARD {n} SCENE UNTUK {durasi} DETIK VIDEO",
            font=f_scene_title, fill=RED)
    fy = sy + title_h

    for i, sc in enumerate(scenes):
        cx = M + i * (col_w + gap)
        fp = frame_paths[i] if i < len(frame_paths) else hero_path
        _paste_cover(img, fp, (cx, fy, cx + col_w, fy + frame_h))
        r = 26
        bx, by = cx + 12, fy + 12
        dr.ellipse([bx, by, bx + 2 * r, by + 2 * r], fill=RED)
        num = str(i + 1)
        twd = dr.textlength(num, font=_font(30, True))
        dr.text((bx + r - twd / 2, by + r - 21), num, font=_font(30, True), fill=WHITE)
        cy = fy + frame_h + 14
        for label, key in (("VISUAL:", "visual_id"), ("CAMERA:", "camera_id"), ("ACTION:", "action_id")):
            dr.text((cx, cy), label, font=f_cap_label, fill=RED)
            cy += 32
            for ln in _wrap(dr, sc.get(key, "-"), f_cap, col_w)[:3]:
                dr.text((cx, cy), ln, font=f_cap, fill=DARK)
                cy += 34
            cy += 10

    img.save(out_path, quality=90)
    return out_path
