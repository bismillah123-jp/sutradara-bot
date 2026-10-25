"""Storyboard v2 - kunci anti-anomali + konsistensi, audio native Flow."""
import base64
import json
import os
import re
import time
import urllib.request
import urllib.error

OMNI_BASE = os.environ.get("OMNI_BASE", "http://192.168.1.7:20128/v1")
OMNI_KEY = os.environ.get("OMNI_API_KEY", "")
OMNI_MODEL = os.environ.get("OMNI_TEXT_MODEL", "bima")

ANATOMY_LOCK = (
    "ANATOMY & PHYSICS: Anatomically correct human hands with exactly five fingers, "
    "natural skin texture and realistic joints. Clean physical interaction with the product, "
    "objects rest naturally on solid surfaces, no floating objects, no clipping, "
    "no extra limbs, no hand warping."
)
ANOMALY_LOCK = ANATOMY_LOCK

CONSISTENCY_LOCK = (
    "CONSISTENCY: The product is 100% identical in every frame to reference image #1 (golden catalog reference) "
    "- exact same form, color, texture, finish, typography, branding, and packaging. "
    "Consistent lighting, mood, and character appearance throughout."
)

MOTION_LOCK = (
    "CAMERA MOTION: Smooth, steady cinematic camera movement executed on a professional gimbal. "
    "One continuous camera movement per shot at uniform speed, no random jerks, no drifting, "
    "no jitter, natural fluid subject motion."
)

PRODUCT_LOCK_TMPL = (
    "PRODUCT FIDELITY: The product is {product}. "
    "It must match the reference image exactly in color, shape, materials, and branding."
)

SCENE_LOGIC_LOCK = (
    "State continuity between shots must be logical: product state, angle, and hands "
    "agree in 3D space with natural physics."
)

LANGUAGE_LOCK = "Spoken narration in Indonesian; video prompts in English."
SPEC_SOURCE_IMAGE = "catalog image (visual ground truth)"
SPEC_SOURCE_DESC = "product description"

FRAME_HINT = {
    "fullbody": "full body shot, the person is visible head to toe",
    "setengah": "half body shot, waist up",
    "tangan": "close-up of hands only holding and using the product, no face visible",
    "produk": "product-only shot, no people, hands may hold the product at the edges",
    "flatlay": "top-down flat lay shot of the product arrangement",
}

AUDIO_LABEL = {
    "vo": "voice over Bahasa Indonesia (narasi diucapkan di dalam video)",
    "asmr": "ASMR, tanpa voice over",
    "musik": "musik saja, tanpa voice over, tanpa narasi lisan",
    "asmr_vo": "ASMR di video + voice over ditambah manual setelahnya (TETAP tulis narasi per beat sebagai skrip VO)",
}

AUDIO_HINT = {
    "vo": "AUDIO: cheerful Indonesian voiceover speaking the exact narration lines provided, natural UGC tone.",
    "asmr": "AUDIO: ASMR style - no voiceover narration, only crisp amplified product sounds (tapping, pouring, clicking, packaging), soft and satisfying.",
    "asmr_vo": "AUDIO: ASMR style - no voiceover narration in the video (the voiceover will be recorded and added manually afterwards), only crisp amplified product sounds (tapping, pouring, clicking, packaging), soft and satisfying.",
    "musik": "AUDIO: upbeat trendy background music only, no speech, no voiceover.",
}

SYSTEM_BOARD = """Kamu adalah sutradara senior video affiliate TikTok Indonesia.

SACRED FLOW:
1. MIND READING: tangkap produk, keunggulan, target penonton, emosi yang menjual.
2. TREATMENT: hook ekstrem di beat pertama, demo yang memuaskan, CTA di beat terakhir.
3. CONTINUITY BIBLE: kunci karakter (usia, wajah, outfit, gaya rambut), kunci produk (bentuk, warna, detail persis gambar referensi), kunci lokasi & cahaya.
4. PRODUCTION: tulis beat per beat.

ATURAN:
- marketing_angle: SATU kalimat tagline catchy Bahasa Indonesia (gaya headline iklan).
- selling_points: 4 poin keunggulan singkat Bahasa Indonesia (maks 7 kata per poin).
- Visual prompt Bahasa Inggris, SATU momen konkret per beat, produk persis seperti deskripsi & gambar referensi.
- visual_id / camera_id / action_id: Bahasa Indonesia, untuk sheet storyboard. camera_id sebutkan angle & gerakan (misal "Close up, push in perlahan").
- MOTION CONTROL (camera_en): arahan kamera PRESISI Bahasa Inggris per beat, format: [shot size] + [SATU gerakan kamera] + [kecepatan] + [arah]. Shot size pilih satu: extreme close-up / close-up / medium shot / wide shot. Gerakan pilih SATU saja: static / slow push in / slow pull out / pan left / pan right / tilt up / tilt down / slow orbit / tracking shot / handheld. DILARANG menggabungkan dua gerakan dalam satu beat, dilarang gerakan yang bertabrakan dengan action subjek.
- action: HANYA aksi subjek/produk (Bahasa Inggris), TANPA gerakan kamera (kamera urusan camera_en).
- Narasi Bahasa Indonesia gaya UGC santai, max 12 kata per beat.
- Beat 1 = hook penahan scroll. Beat terakhir = CTA beli (cek keranjang kuning / checkout sekarang).

OUTPUT JSON SAJA:
{"title": "judul video",
 "marketing_angle": "tagline catchy Bahasa Indonesia",
 "selling_points": ["poin 1", "poin 2", "poin 3", "poin 4"],
 "target_audience": "target penonton Bahasa Indonesia",
 "objective": "tujuan video Bahasa Indonesia, 1 kalimat",
 "character_lock": "deskripsi karakter yang dikunci (Bahasa Inggris)",
 "scenes": [
   {"judul": "judul beat",
    "visual_prompt": "English image prompt for this storyboard frame",
    "action": "English: subject/product action ONLY, no camera movement",
    "camera_en": "English: [shot size], [one camera movement], [speed]",
    "visual_id": "deskripsi visual Bahasa Indonesia",
    "camera_id": "angle & gerakan kamera Bahasa Indonesia",
    "action_id": "aksi Bahasa Indonesia",
    "narasi": "kalimat Bahasa Indonesia max 12 kata"}
 ]}"""


def _sse_to_text(raw: str) -> str:
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            d = json.loads(payload)
        except json.JSONDecodeError:
            continue
        for ch in d.get("choices", []):
            delta = ch.get("delta", {}) or {}
            c = delta.get("content")
            if c:
                out.append(c)
    return "".join(out)


def _call_omni(system: str, user: str) -> str:
    url = f"{OMNI_BASE}/chat/completions"
    body = json.dumps({
        "model": OMNI_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.9,
        "response_format": {"type": "json_object"},
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    if OMNI_KEY:
        headers["Authorization"] = f"Bearer {OMNI_KEY}"
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=180) as r:
                raw = r.read().decode("utf-8", "replace")
            ctype = r.headers.get("Content-Type", "")
            if "text/event-stream" in ctype or raw.lstrip().startswith("data:"):
                text = _sse_to_text(raw)
            else:
                d = json.loads(raw)
                text = d["choices"][0]["message"]["content"]
            if text.strip():
                return text
            last_err = RuntimeError("respons kosong dari model")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (403, 429, 500, 502, 503) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            raise
    raise last_err or RuntimeError("omni API gagal")


VARIANT_DETECT_SYSTEM = (
    "Kamu adalah analis gambar katalog produk. "
    "Jawab SELALU hanya dengan JSON valid, tanpa teks lain."
)

VARIANT_DETECT_USER = (
    "Analisis gambar katalog produk ini. Apakah ada LEBIH DARI 1 varian produk "
    "yang terlihat (beda warna/model)? "
    'Jawab JSON: {"multi": true/false, "varian": '
    '["deskripsi singkat varian 1", "deskripsi singkat varian 2", ...]}'
)


def analyze_catalog_variants(image_path):
    """Deteksi multi-varian katalog via bima vision.

    Mengembalikan dict {"multi": bool, "varian": [str, ...]}.
    Aman gagal: jika apapun gagal, kembalikan {"multi": False, "varian": []}
    sehingga alur katalog single-varian tidak terganggu.
    """
    kosong = {"multi": False, "varian": []}
    try:
        with open(image_path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        mime = "image/png" if str(image_path).lower().endswith(".png") else "image/jpeg"
        data_url = "data:" + mime + ";base64," + b64
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "sutradara-bot/2.0",
            "Accept": "application/json, text/event-stream",
        }
        if OMNI_KEY:
            headers["Authorization"] = "Bearer " + OMNI_KEY
        payload = {
            "model": OMNI_MODEL,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": VARIANT_DETECT_SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": VARIANT_DETECT_USER},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]},
            ],
        }
        req = urllib.request.Request(
            OMNI_BASE + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=90) as r:
            raw = r.read().decode("utf-8", "replace")
            ctype = r.headers.get("Content-Type", "")
        if "text/event-stream" in ctype or raw.lstrip().startswith("data:"):
            teks = _sse_to_text(raw)
        else:
            teks = json.loads(raw)["choices"][0]["message"]["content"]
        teks = (teks or "").strip()
        if teks.startswith("```"):
            teks = teks.strip("`").strip()
            if teks[:4].lower() == "json":
                teks = teks[4:].strip()
        hasil = json.loads(teks)
        varian = hasil.get("varian") or []
        varian = [str(v).strip() for v in varian if str(v).strip()]
        multi = bool(hasil.get("multi")) and len(varian) > 1
        return {"multi": multi, "varian": varian}
    except Exception:
        return kosong


async def generate_storyboard(produk: str, deskripsi: str, durasi: int,
                              style: str, audio: str, framing: str,
                              katalog_path: str = None,
                              vision_spec: dict = None) -> dict:
    total_segments = max(1, int(durasi) // 8)
    n_scene = max(2, total_segments * 2)
    user = (
        f"Produk: {produk}\nDeskripsi produk: {deskripsi}\n"
        f"Durasi total video: {durasi} detik ({total_segments} segmen @ 8 detik).\n"
        f"Tulis TEPAT {n_scene} beat cerita berurutan (2 beat per segmen 8 detik, masing-masing 4 detik).\n"
        f"PENTING: Alur cerita harus MAJU dan BERBEDA di setiap beat. DILARANG MENGULANG adegan/aksi yang sama!\n"
        f"Beat 1-2: Hook & pembuka dramatis memperkenalkan {produk}.\n"
        + (f"Beat 3-4: Demonstrasi fungsi utama dan detail produk secara nyata.\n" if total_segments >= 2 else "")
        + (f"Beat 5-6: Hasil memuaskan, variasi pemakaian, dan Call to Action beli di keranjang kuning.\n" if total_segments >= 3 else (f"Beat {n_scene}: Call to Action beli di keranjang kuning.\n" if total_segments == 1 else f"Beat 4: Call to Action beli di keranjang kuning.\n"))
        + f"Style visual: {style}. {STYLE_BOARD_GUIDE.get(style, '')} Framing: {FRAME_HINT.get(framing, '')}.\n"
        f"Mode audio: {AUDIO_LABEL.get(audio, audio)}.\n"
        f"Product visual spec: output product_spec dict dengan keys: color, material, shape, size, count, key_features."
    )
    raw = _call_omni(SYSTEM_BOARD, user)
    raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
    raw = re.sub(r"\n?```$", "", raw.strip())
    m = re.search(r"\{.*\}", raw, re.S)
    board = json.loads(m.group(0) if m else raw)
    assert board.get("scenes"), "AI tidak menghasilkan scene"
    board.setdefault("character_lock", "")
    board.setdefault("marketing_angle", board.get("title", ""))
    board.setdefault("selling_points", [])
    board.setdefault("target_audience", "Pengguna TikTok Indonesia")
    board.setdefault("objective", "Meningkatkan penjualan produk")
    for sc in board["scenes"]:
        for k in ("visual_prompt", "action", "narasi", "judul",
                  "visual_id", "camera_id", "action_id", "camera_en"):
            sc.setdefault(k, "")
        if not sc["visual_id"]:
            sc["visual_id"] = sc["judul"]
        if not sc["action_id"]:
            sc["action_id"] = sc["narasi"]
        if not sc["camera_id"]:
            sc["camera_id"] = "Close up"
        if not sc["camera_en"]:
            sc["camera_en"] = "Close-up, static camera, perfectly steady"
    spec_text = build_product_spec(produk, deskripsi, board.get("product_spec") or {}, vision_spec)
    board["product_spec_text"] = spec_text
    return board


STYLE_DIRECTING = {
    "ugc": "STYLE: authentic UGC handheld footage. Slightly shaky natural hand movements, casual indoor lighting (like room lamp or window light), realistic everyday background (desk, bedroom, kitchen), no cinematic polish, feels like a real person filming with their phone. Warm approachable vibe.",
    "sinematik": "STYLE: cinematic premium commercial. Dramatic studio lighting with rim light, shallow depth of field, slow deliberate camera moves, rich saturated colors, dark moody background, high-budget TV commercial feel.",
    "studio": "STYLE: clean studio product photography in motion. Bright even softbox lighting, seamless white/light-gray background, crisp shadows, sharp focus, minimalist e-commerce aesthetic, professional catalog video.",
    "aesthetic": "STYLE: soft aesthetic pastel mood. Dreamy diffused natural light, pastel color palette (soft pink, cream, sage), gentle slow motion feel, cozy beautiful setting with plants or fabric textures, Instagram-aesthetic vibe.",
}

STYLE_CLOSING = {
    "ugc": "The final result must feel authentic and unpolished, like a real person filming with their phone.",
    "sinematik": "The final result must look like a high-budget TV commercial.",
    "studio": "The final result must look like a professional e-commerce catalog video.",
    "aesthetic": "The final result must look like a beautiful Instagram-aesthetic video.",
}

STYLE_BOARD_GUIDE = {
    "ugc": "Direct the beats like casual everyday moments: realistic home settings, natural unscripted actions, handheld phone-camera feel.",
    "sinematik": "Direct the beats like a dramatic premium commercial: bold lighting moments, slow deliberate camera moves, cinematic product reveals.",
    "studio": "Direct the beats like a clean studio shoot: bright minimal setting, simple product-focused actions, crisp catalog look.",
    "aesthetic": "Direct the beats like a soft dreamy mood film: cozy beautiful settings, gentle slow movements, pastel Instagram-aesthetic vibe.",
}

def build_frame_prompt(sc: dict, spec_text: str = "",
                       style: str = "sinematik", framing: str = "tangan") -> str:
    """Prompt for generating a single storyboard reference frame (9:16)."""
    base = sc.get("visual_prompt") or sc.get("judul") or "Product commercial shot"
    style_desc = STYLE_DIRECTING.get(style, STYLE_DIRECTING.get("sinematik", ""))
    frame_desc = FRAME_HINT.get(framing, "")
    parts = [
        f"High quality 9:16 vertical commercial photo. {base}.",
        f"Style & Framing: {style_desc} {frame_desc}".strip(),
    ]
    if spec_text:
        parts.append(f"Product fidelity: {spec_text}")
    parts.append(ANATOMY_LOCK)
    parts.append("Clean composition, professional studio lighting, centered subject, crystal sharp details, no text, no watermark.")
    return "\n".join(parts)


def build_segment_prompts(board: dict, produk: str, durasi: int,
                          style: str = "sinematik", audio: str = "vo",
                          framing: str = "tangan", golden_ref: bool = True) -> list:
    """Build clean, targeted English prompts per 8-second Veo segment.

    Prevents scene repetition and visual looping by strictly partitioning beats
    across segments:
    - 8s (1 segment): Shots 1-2 (0-8s)
    - 16s (2 segments): Segment 1 -> Shots 1-2 (0-8s); Segment 2 -> Shots 3-4 (8-16s)
    - 24s (3 segments): Segment 1 -> Shots 1-2; Segment 2 -> Shots 3-4; Segment 3 -> Shots 5-6
    """
    scenes = board.get("scenes") or []
    if not scenes:
        scenes = [{
            "judul": "Product Showcase",
            "visual_prompt": f"Close-up commercial showcase of {produk}",
            "action": f"Demonstrating {produk} features smoothly",
            "camera_en": "Close-up, slow push in, steady",
            "narasi": "",
        }]

    total_segments = max(1, int(durasi) // 8)
    n = len(scenes)
    step = n / total_segments

    spec_text = board.get("product_spec_text") or build_product_spec(produk, "", None, None)
    style_desc = STYLE_DIRECTING.get(style, STYLE_DIRECTING.get("sinematik", ""))
    frame_desc = FRAME_HINT.get(framing, "")
    audio_desc = AUDIO_HINT.get(audio, AUDIO_HINT.get("vo", ""))

    segment_prompts = []

    for k in range(total_segments):
        start_idx = int(round(k * step))
        end_idx = n if k == total_segments - 1 else int(round((k + 1) * step))
        seg_scenes = scenes[start_idx:end_idx]
        if not seg_scenes:
            seg_scenes = scenes[-1:]

        seg_t_start = k * 8
        seg_t_end = (k + 1) * 8

        parts = []
        if total_segments == 1:
            parts.append(
                f"Create an 8-second vertical 9:16 TikTok commercial video for: {produk}."
            )
        else:
            if k == 0:
                parts.append(
                    f"Create an 8-second vertical 9:16 TikTok commercial video [Segment 1 of {total_segments}, 0-8s] for: {produk}."
                )
            else:
                parts.append(
                    f"Create an 8-second vertical 9:16 TikTok commercial video [Segment {k+1} of {total_segments}, {seg_t_start}-{seg_t_end}s] for: {produk}.\n"
                    "CONTINUITY: Seamless visual and physical continuity from the attached previous scene reference. "
                    "Maintain exact same character, outfit, product position, background environment, and lighting. "
                    "Continue the action forward naturally without restarting or repeating earlier actions."
                )

        parts.append(f"Style: {style_desc} Framing: {frame_desc}".strip())
        parts.append(f"Product fidelity (match reference image #1): {spec_text}")

        if board.get("character_lock"):
            parts.append(f"Character: {board['character_lock']}")

        parts.append("SHOTS TO EXECUTE IN THIS 8-SECOND SEGMENT:")
        m = len(seg_scenes)
        for j, sc in enumerate(seg_scenes):
            shot_start = int(round(j * (8.0 / m)))
            shot_end = 8 if j == m - 1 else int(round((j + 1) * (8.0 / m)))
            shot_num = start_idx + j + 1
            vis = sc.get("visual_prompt", "").strip() or sc.get("judul", "Showcase")
            act = sc.get("action", "").strip()
            cam = sc.get("camera_en", "").strip() or "Close-up, steady gimbal movement"
            parts.append(
                f"- Shot {shot_num} ({shot_start}-{shot_end}s in this segment): {vis}. Action: {act}. Camera: {cam}."
            )

        if audio_desc:
            parts.append(audio_desc)

        parts.append(ANATOMY_LOCK)
        parts.append(MOTION_LOCK)
        parts.append("Final result: Razor-sharp product focus, seamless motion, zero visual artifacts, television commercial grade.")

        segment_prompts.append("\n\n".join(parts))

    return segment_prompts


def build_video_prompt(board: dict, produk: str, durasi: int,
                       style: str, audio: str, framing: str,
                       golden_ref: bool = True):
    """Returns per-segment prompt list for multi-segment videos or single string if 8s."""
    prompts = build_segment_prompts(board, produk, durasi, style, audio, framing, golden_ref)
    if durasi <= 8 and len(prompts) == 1:
        return prompts[0]
    return prompts


ENHANCE_PROMPT = (
    "Enhance this product photo into a clean HD e-commerce catalog image: "
    "the product is razor sharp and exactly identical in shape, color and details. "
    "CRITICAL FIDELITY: preserve the EXACT appearance of EVERY part of the product - "
    "color, material, texture, and finish. A metal part stays metal, plastic stays plastic, "
    "glass stays glass, fabric stays fabric. Matte stays matte, glossy stays glossy. "
    "DO NOT change materials, DO NOT shift colors brighter or darker, DO NOT add, remove, "
    "or redesign any part. What you see in the original is EXACTLY what must appear - "
    "only sharper and cleaner, never different. "
    "Studio lighting, clean simple background, remove all clutter and unimportant objects, "
    "no people, no text, no watermark. Vertical 9:16."
)


def catalog_prompt(produk: str, deskripsi: str) -> str:
    return (
        f"Clean HD e-commerce catalog product photo of: {produk}. {deskripsi} "
        "Studio lighting, simple clean background, product centered and razor sharp, "
        "no people, no text, no watermark. Vertical 9:16."
    )



# ---------- visual hint extraction (Indonesian text -> English spec) ----------

_ID_COLORS = [
    ("putih", "white", "#FFFFFF"),
    ("hitam", "black", "#111111"),
    ("abu-abu", "gray", "#808080"),
    ("abu", "gray", "#808080"),
    ("silver", "silver", "#C0C0C0"),
    ("perak", "silver", "#C0C0C0"),
    ("emas", "gold", "#D4AF37"),
    ("gold", "gold", "#D4AF37"),
    ("merah muda", "pink", "#FFC0CB"),
    ("pink", "pink", "#FFC0CB"),
    ("merah", "red", "#C00000"),
    ("marun", "maroon", "#800000"),
    ("oranye", "orange", "#FF8000"),
    ("orange", "orange", "#FF8000"),
    ("kuning", "yellow", "#FFD700"),
    ("hijau", "green", "#008000"),
    ("toska", "teal", "#008080"),
    ("biru", "blue", "#0050C8"),
    ("navy", "navy blue", "#000080"),
    ("ungu", "purple", "#800080"),
    ("coklat", "brown", "#8B5A2B"),
    ("cokelat", "brown", "#8B5A2B"),
    ("krem", "cream", "#FFFDD0"),
    ("beige", "beige", "#F5F5DC"),
    ("transparan", "transparent", None),
    ("bening", "transparent", None),
]

_ID_MATERIALS = [
    ("stainless", "stainless steel"),
    ("aluminium", "aluminum"),
    ("aluminum", "aluminum"),
    ("logam", "metal"),
    ("metal", "metal"),
    ("plastik", "plastic"),
    ("silikon", "silicone"),
    ("karet", "rubber"),
    ("kaca", "glass"),
    ("kayu", "wood"),
    ("kulit", "leather"),
    ("kain", "fabric"),
    ("kanvas", "canvas"),
    ("keramik", "ceramic"),
    ("kertas", "paper"),
    ("kardus", "cardboard"),
]


def _extract_color_id(text: str) -> str:
    t = (text or "").lower()
    for indo, eng, hx in _ID_COLORS:
        if re.search(r"\b" + re.escape(indo) + r"\b", t):
            return f"{eng} ({hx})" if hx else eng
    return ""


def _extract_material_id(text: str) -> str:
    t = (text or "").lower()
    found = []
    for indo, eng in _ID_MATERIALS:
        if re.search(r"\b" + re.escape(indo) + r"\b", t):
            if eng not in found:
                found.append(eng)
    return ", ".join(found)


def _extract_count_id(text: str) -> str:
    t = (text or "").lower()
    m = re.search(r"(?:isi|jumlah|terdiri dari|berisi)\s*(\d+)", t)
    if m:
        return m.group(1) + " units"
    m = re.search(r"(\d+)\s*(pcs|buah|unit|pasang|set|pak|botol|kaleng|sachet|bungkus)", t)
    if m:
        n = m.group(1)
        return n + (" pairs" if m.group(2) == "pasang" else " units")
    if re.search(r"\bsepasang\b", t):
        return "1 pair (2 units)"
    return ""


def extract_visual_hints(deskripsi: str) -> dict:
    """Heuristic visual hints from Indonesian description text."""
    d = deskripsi or ""
    return {
        "color": _extract_color_id(d),
        "material": _extract_material_id(d),
        "count": _extract_count_id(d),
    }


# ---------- vision-based product spec (visual ground truth) ----------

SPEC_VISION_SYSTEM = (
    "You are a precise product-vision analyst. Analyze ONLY the product photo. "
    "Describe exactly what you see - never guess or invent details. "
    "If a detail is not clearly visible, write \"not specified\". "
    "Output JSON only."
)

SPEC_VISION_USER = (
    "Analyze the product in this photo. Output JSON with keys: "
    "color, material, shape, size, count, key_features. "
    "For color, give the exact dominant color. For count, say how many "
    "product units are visible. For key_features, list logo, buttons, "
    "screen, ports, labels. "
    "Rules: describe ONLY what is visible. Never invent details. "
    "If a detail is not clearly visible, write not specified."
)


def analyze_product_visual_spec(image_path: str) -> dict:
    """Extract visual spec from catalog image. Image wins over text."""
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        ext = os.path.splitext(image_path)[1].lower().lstrip(".") or "jpg"
        if ext == "jpg":
            ext = "jpeg"
        payload = {
            "model": OMNI_MODEL,
            "messages": [
                {"role": "system", "content": SPEC_VISION_SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": SPEC_VISION_USER},
                    {"type": "image_url", "image_url":
                        {"url": f"data:image/{ext};base64,{b64}"}},
                ]},
            ],
            "temperature": 0.1,
        }
        req = urllib.request.Request(
            OMNI_BASE + "/chat/completions", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {OMNI_KEY}"},
            method="POST")
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode())
        raw = data["choices"][0]["message"]["content"] or ""
        m = re.search(r"{.*}", raw, re.S)
        spec = json.loads(m.group(0) if m else raw)
        return {k: str(spec.get(k, "") or "").strip() for k in
                ("color", "material", "shape", "size", "count",
                 "key_features")}
    except Exception:
        return {}


# ---------- UNIVERSAL PRODUCT SPEC BLOCK ----------

_NOT_SPECIFIED = "not specified"


def _clean_spec(v) -> str:
    v = str(v or "").strip()
    return v if v and v.lower() != _NOT_SPECIFIED else ""


def build_product_spec(produk: str, deskripsi: str = "",
                       ai_spec: dict = None,
                       vision_spec: dict = None) -> str:
    """Build concise product spec for diffusion model conditioning."""
    ai_spec = ai_spec or {}
    vision_spec = vision_spec or {}
    hints = extract_visual_hints(deskripsi or "")

    def pick(field):
        for src in (vision_spec, ai_spec):
            v = _clean_spec(src.get(field))
            if v:
                return v
        return _clean_spec(hints.get(field))

    items = [f"Product: {produk or 'featured item'}"]
    for label, field in [
        ("Color", "color"),
        ("Material", "material"),
        ("Shape", "shape"),
        ("Size", "size"),
        ("Count", "count"),
        ("Key features", "key_features"),
    ]:
        val = pick(field)
        if val:
            items.append(f"{label}: {val}")

    return " | ".join(items)


def build_scene_prompt(visual_prompt: str, spec_text: str = "") -> str:
    """Compose ONE scene prompt: visual + PRODUCT SPEC + locks."""
    parts = [visual_prompt or ""]
    if spec_text:
        parts.append(spec_text)
    parts += [CONSISTENCY_LOCK, ANOMALY_LOCK]
    return "\n".join(parts)