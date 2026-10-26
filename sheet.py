"""Komposer sheet storyboard (PIL) - format: Marketing Angle + info + scene grid ultra HD."""
import os
from PIL import Image, ImageDraw, ImageFont

# Color palette: High-end commercial studio & agency
RED = (225, 29, 72)          # TikTok / Rose signature red
DARK = (15, 23, 42)          # Slate 900
GRAY = (71, 85, 105)         # Slate 600
LIGHT_GRAY = (148, 163, 184) # Slate 400
GREEN = (16, 185, 129)       # Emerald green for checks
PANEL = (248, 250, 252)      # Slate 50 clean panel
BORDER = (226, 232, 240)     # Slate 200 border
WHITE = (255, 255, 255)
PILL_BG = (255, 241, 242)    # Rose 50 subtle pill

_FONT_CACHE = {}


def _find_font_path(bold: bool = False):
    candidates = [
        # Linux / Ubuntu / Debian paths (VPS)
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf" if bold else "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf" if bold else "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/droid/DroidSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/droid/DroidSans.ttf",
        # Windows paths
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibrib.ttf" if bold else "C:/Windows/Fonts/calibri.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _font(size: int, bold: bool = False):
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    fp = _find_font_path(bold)
    if fp:
        try:
            f = ImageFont.truetype(fp, size)
            _FONT_CACHE[key] = f
            return f
        except Exception:
            pass
    for name in ("Arial", "DejaVuSans", "Helvetica", "sans-serif"):
        try:
            f = ImageFont.truetype(name, size)
            _FONT_CACHE[key] = f
            return f
        except Exception:
            pass
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def _wrap(draw, text: str, font, max_w: int):
    if not text:
        return [""]
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        t = f"{cur} {w}".strip() if cur else w
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _paste_rounded_frame(canvas, path: str, box, radius=16, border_color=(226, 232, 240), border_width=2):
    x0, y0, x1, y1 = box
    tw, th = max(1, x1 - x0), max(1, y1 - y0)
    try:
        im = Image.open(path).convert("RGBA")
    except Exception:
        im = Image.new("RGBA", (tw, th), (220, 225, 230, 255))

    scale = max(tw / im.width, th / im.height)
    nw, nh = int(im.width * scale + 0.5), int(im.height * scale + 0.5)
    im = im.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - tw) // 2, (nh - th) // 2
    im = im.crop((left, top, left + tw, top + th))

    # Mask with rounded corners
    mask = Image.new("L", (tw, th), 0)
    draw_mask = ImageDraw.Draw(mask)
    draw_mask.rounded_rectangle([0, 0, tw, th], radius=radius, fill=255)

    if border_width > 0 and border_color:
        border_canvas = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        draw_b = ImageDraw.Draw(border_canvas)
        draw_b.rounded_rectangle([0, 0, tw - 1, th - 1], radius=radius, outline=border_color, width=border_width)
        im = Image.alpha_composite(im, border_canvas)

    canvas.paste(im.convert("RGB"), (x0, y0), mask)


def compose_sheet(board: dict, produk: str, jenis: str, durasi: int,
                  hero_path: str, frame_paths: list, out_path: str,
                  style: str = "sinematik", audio: str = "vo"):
    """Komposisi storyboard sheet beresolusi tinggi dengan layout adaptive grid."""
    scenes = board.get("scenes") or []
    n = len(scenes)
    if n == 0:
        return out_path

    W = 1800
    M = 48
    gap = 24
    tmp = Image.new("RGB", (10, 10))
    d = ImageDraw.Draw(tmp)

    f_super = _font(22, True)
    f_tag = _font(48, True)
    f_bullet = _font(28)
    f_label = _font(24, True)
    f_value = _font(26)
    f_badge = _font(28, True)
    f_scene_title = _font(38, True)
    f_card_title = _font(32, True)
    f_cap_label = _font(22, True)
    f_cap = _font(24)

    # 1. Header calculation
    left_w = 980
    tag_str = board.get("marketing_angle", "") or board.get("title", produk)
    tag_lines = _wrap(d, f"“{tag_str}”", f_tag, left_w)
    bullet_lines = []
    for sp in board.get("selling_points", [])[:4]:
        bullet_lines.extend(_wrap(d, sp, f_bullet, left_w - 52))

    header_h = 76 + len(tag_lines) * 62 + 24 + len(bullet_lines) * 38 + 36
    header_h = max(header_h, 480)

    # 2. Adaptive Grid Configuration
    if n <= 4:
        cols = 2
        rows = (n + 1) // 2
        card_w = (W - 2 * M - gap) // 2  # 840 px wide
        card_h = 740
        use_side_by_side = True
    else:
        cols = 3
        rows = (n + 2) // 3
        card_w = (W - 2 * M - 2 * gap) // 3  # 544 px wide
        card_h = 1080
        use_side_by_side = False

    title_h = 80
    cards_total_h = rows * card_h + (rows - 1) * gap
    footer_h = 90
    H = 36 + header_h + 36 + title_h + cards_total_h + footer_h + 40

    img = Image.new("RGB", (W, H), WHITE)
    dr = ImageDraw.Draw(img)

    # Header Box
    dr.rounded_rectangle([M, 36, W - M, 36 + header_h], radius=24, fill=PANEL, outline=BORDER, width=2)

    # Header Left: Marketing Angle & Selling points
    x, y = M + 36, 68
    dr.text((x, y), "★ MARKETING ANGLE & HOOK BIBLE", font=f_super, fill=RED)
    y += 42
    for ln in tag_lines:
        dr.text((x, y), ln, font=f_tag, fill=DARK)
        y += 62
    y += 16
    for sp in board.get("selling_points", [])[:4]:
        lines = _wrap(dr, sp, f_bullet, left_w - 52)
        for j, ln in enumerate(lines):
            if j == 0:
                dr.ellipse([x, y + 4, x + 24, y + 28], fill=GREEN)
                dr.text((x + 6, y + 1), "✓", font=_font(18, True), fill=WHITE)
            dr.text((x + 38, y), ln, font=f_bullet, fill=GRAY)
            y += 38

    # Header Right: Hero Image + Metadata
    hero_x0 = M + left_w + 32
    hero_w = 320
    hero_h = header_h - 72
    _paste_rounded_frame(img, hero_path, (hero_x0, 68, hero_x0 + hero_w, 68 + hero_h),
                         radius=16, border_color=BORDER, border_width=2)
    dr.rounded_rectangle([hero_x0 + 16, 68 + hero_h - 40, hero_x0 + hero_w - 16, 68 + hero_h - 10],
                         radius=12, fill=DARK)
    t_ref = "👑 KATALOG ACUAN"
    t_ref_w = dr.textlength(t_ref, font=_font(18, True))
    dr.text((hero_x0 + (hero_w - t_ref_w) / 2, 68 + hero_h - 35), t_ref, font=_font(18, True), fill=WHITE)

    ix = hero_x0 + hero_w + 32
    iy = 68
    info_max_w = W - M - ix - 24
    infos = [
        ("PRODUK:", produk),
        ("FORMAT:", "9:16 Vertical Video"),
        ("DURASI:", f"{durasi} Detik ({n} Beat Scene)"),
        ("TARGET AUDIENCE:", board.get("target_audience", "-")),
        ("OBJECTIVE:", board.get("objective", "-")),
    ]
    for label, value in infos:
        dr.text((ix, iy), label, font=f_label, fill=RED)
        iy += 32
        for ln in _wrap(dr, value, f_value, info_max_w)[:2]:
            dr.text((ix, iy), ln, font=f_value, fill=DARK)
            iy += 32
        iy += 12

    # Section Title
    sy = 36 + header_h + 36
    dr.text((M, sy), f"STORYBOARD VISUAL • {n} BEAT SCENE ({durasi} DETIK TIKTOK)",
            font=f_scene_title, fill=DARK)
    dr.text((M, sy + 44), "Setiap frame dibuat ultra-tajam dengan konsistensi produk 100% dan alur hook affiliate.",
            font=f_cap, fill=GRAY)

    fy = sy + title_h
    dur_beat = max(1, durasi // n)

    for i, sc in enumerate(scenes):
        row = i // cols
        col = i % cols
        cx = M + col * (card_w + gap)
        cy = fy + row * (card_h + gap)

        dr.rounded_rectangle([cx, cy, cx + card_w, cy + card_h],
                             radius=20, fill=PANEL, outline=BORDER, width=2)

        fp = frame_paths[i] if i < len(frame_paths) and frame_paths[i] else hero_path
        t_start = i * dur_beat
        t_end = durasi if i == n - 1 else (i + 1) * dur_beat

        if use_side_by_side:
            # 2-column layout: side-by-side (Left: 9:16 frame, Right: specs)
            img_x = cx + 18
            img_y = cy + 18
            img_w = 360
            img_h = 704
            _paste_rounded_frame(img, fp, (img_x, img_y, img_x + img_w, img_y + img_h),
                                 radius=14, border_color=BORDER, border_width=2)

            # Circular number badge
            badge_r = 24
            dr.ellipse([img_x + 14, img_y + 14, img_x + 14 + 2 * badge_r, img_y + 14 + 2 * badge_r], fill=RED)
            num_str = str(i + 1)
            nw = dr.textlength(num_str, font=f_badge)
            dr.text((img_x + 14 + badge_r - nw / 2, img_y + 14 + 8), num_str, font=f_badge, fill=WHITE)

            # Specs column on right
            sx = img_x + img_w + 24
            sy_cur = cy + 22
            sw = card_w - img_w - 56

            # Timing pill
            dr.rounded_rectangle([sx, sy_cur, sx + 220, sy_cur + 36], radius=10, fill=PILL_BG)
            dr.text((sx + 14, sy_cur + 6), f"BEAT {i+1} • {t_start}-{t_end}s", font=f_label, fill=RED)
            sy_cur += 50

            # Title
            judul = sc.get("judul", f"Scene {i+1}")
            for ln in _wrap(dr, judul, f_card_title, sw)[:2]:
                dr.text((sx, sy_cur), ln, font=f_card_title, fill=DARK)
                sy_cur += 40
            sy_cur += 12

            # Visual, Camera, Action, VO
            items = [
                ("👁️ VISUAL", sc.get("visual_id") or sc.get("visual_prompt", "-")),
                ("🎥 KAMERA", sc.get("camera_id") or sc.get("camera_en", "-")),
                ("⚡ AKSI", sc.get("action_id") or sc.get("action", "-")),
                ("🗣️ SKRIP / VO", sc.get("narasi", "") or ("🎧 Audio ASMR Foley (Silent)" if audio in ("asmr", "musik") else "-")),
            ]
            for lbl, val in items:
                dr.text((sx, sy_cur), lbl, font=f_cap_label, fill=RED)
                sy_cur += 28
                for ln in _wrap(dr, val, f_cap, sw)[:3]:
                    dr.text((sx, sy_cur), ln, font=f_cap, fill=DARK)
                    sy_cur += 30
                sy_cur += 8

        else:
            # 3-column layout: vertical stacked
            img_x = cx + 18
            img_y = cy + 18
            img_w = card_w - 36
            img_h = 670
            _paste_rounded_frame(img, fp, (img_x, img_y, img_x + img_w, img_y + img_h),
                                 radius=14, border_color=BORDER, border_width=2)

            badge_r = 24
            dr.ellipse([img_x + 14, img_y + 14, img_x + 14 + 2 * badge_r, img_y + 14 + 2 * badge_r], fill=RED)
            num_str = str(i + 1)
            nw = dr.textlength(num_str, font=f_badge)
            dr.text((img_x + 14 + badge_r - nw / 2, img_y + 14 + 8), num_str, font=f_badge, fill=WHITE)

            sx = cx + 20
            sy_cur = img_y + img_h + 16
            sw = card_w - 40

            # Timing pill
            dr.rounded_rectangle([sx, sy_cur, sx + 190, sy_cur + 34], radius=10, fill=PILL_BG)
            dr.text((sx + 14, sy_cur + 6), f"BEAT {i+1} • {t_start}-{t_end}s", font=f_label, fill=RED)
            sy_cur += 44

            items = [
                ("👁️ VISUAL", sc.get("visual_id") or sc.get("visual_prompt", "-")),
                ("🎥 KAMERA", sc.get("camera_id") or sc.get("camera_en", "-")),
                ("⚡ AKSI", sc.get("action_id") or sc.get("action", "-")),
                ("🗣️ SKRIP / VO", sc.get("narasi", "") or ("🎧 Audio ASMR Foley (Silent)" if audio in ("asmr", "musik") else "-")),
            ]
            for lbl, val in items:
                dr.text((sx, sy_cur), lbl, font=f_cap_label, fill=RED)
                sy_cur += 26
                for ln in _wrap(dr, val, f_cap, sw)[:2]:
                    dr.text((sx, sy_cur), ln, font=f_cap, fill=DARK)
                    sy_cur += 28
                sy_cur += 6

    # Footer Banner
    footer_y = H - footer_h - 20
    dr.rounded_rectangle([M, footer_y, W - M, footer_y + footer_h], radius=16, fill=DARK)
    f_foot_title = _font(24, True)
    f_foot_sub = _font(20)
    foot_t = "SUTRADARA AI • TIKTOK AFFILIATE PRODUCTION ENGINE • ULTRA HD 9:16"
    foot_sub = "Format storyboard sinematik berstandar iklan komersial TV dengan retensi tinggi & hook scroll-stopping."
    dr.text((M + 32, footer_y + 18), foot_t, font=f_foot_title, fill=WHITE)
    dr.text((M + 32, footer_y + 52), foot_sub, font=f_foot_sub, fill=LIGHT_GRAY)

    img.save(out_path, quality=95, subsampling=0)
    return out_path
