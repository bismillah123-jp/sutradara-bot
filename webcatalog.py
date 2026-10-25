"""Cari katalog mirip di web + bedah link produk.
Pakai TinyFish Fetch (POST https://api.fetch.tinyfish.ai, GRATIS) + bima."""
import base64
import io
import json
import logging
import os
import re
import tempfile
import urllib.parse
import urllib.request

log = logging.getLogger("webcatalog")

from research import _tinyfish_key, _search
from storyboard import _call_omni

FETCH_URL = "https://api.fetch.tinyfish.ai"
_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0 Safari/537.36")}

_OG_RE = re.compile(
    r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)',
    re.I)
_TW_RE = re.compile(
    r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)',
    re.I)
_JUNK = ("logo", "icon", "sprite", "banner", "placeholder", "avatar",
         "favicon", "pixel", "badge", "loading", "spinner", "qr")


def tinyfish_fetch(urls: list[str], want_images: bool = True):
    """Fetch 1-10 URL paralel via TinyFish. Return (results, errors)."""
    key = _tinyfish_key()
    if not key:
        raise RuntimeError("TinyFish API key tidak ada")
    body = json.dumps({"urls": urls[:10], "format": "html",
                       "image_links": want_images, "ttl": 0}).encode()
    req = urllib.request.Request(
        FETCH_URL, data=body,
        headers={"X-API-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    return d.get("results") or [], d.get("errors") or []


def _abs_url(u: str, base: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        u = "https:" + u
    elif u.startswith("/"):
        u = urllib.parse.urljoin(base, u)
    return u


def gambar_dari_hasil(res: dict) -> list[str]:
    """Ambil URL gambar produk dari satu hasil fetch (og:image + image_links)."""
    base = res.get("final_url") or res.get("url") or ""
    html = res.get("text") or ""
    if isinstance(html, dict):
        html = ""
    out = []
    for rx in (_OG_RE, _TW_RE):
        m = rx.search(html)
        if m:
            out.append(_abs_url(m.group(1), base))
    for im in (res.get("image_links") or []):
        u = im.get("url") if isinstance(im, dict) else im
        u = _abs_url(u, base)
        if not u:
            continue
        if any(j in u.lower() for j in _JUNK):
            continue
        out.append(u)
    seen, final = set(), []
    for u in out:
        if u and u not in seen:
            seen.add(u)
            final.append(u)
    return final

# ---------------------------------------------------------------------------
# Google Custom Search (sumber UTAMA pencarian katalog web).
# Kredensial diisi user via /setgoogle <api_key> <cx>, disimpan di
# billing_config.json. Key kosong = nonaktif, langsung fallback ke TinyFish.
# ---------------------------------------------------------------------------
GOOGLE_CSE_URL = "https://www.googleapis.com/customsearch/v1"
_BILLING_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "billing_config.json")


def _baca_billing_config():
    try:
        with open(_BILLING_CONFIG, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _google_key():
    return str(_baca_billing_config().get("GOOGLE_API_KEY") or "").strip()


def _google_cx():
    return str(_baca_billing_config().get("GOOGLE_CX") or "").strip()


def google_image_search(query, api_key, cx, num=5):
    """Cari gambar via Google Custom Search API. Return list URL gambar."""
    if not query or not api_key or not cx:
        return []
    try:
        n = max(1, min(int(num or 5), 10))
    except (TypeError, ValueError):
        n = 5
    params = urllib.parse.urlencode({"q": query, "cx": cx, "key": api_key, "searchType": "image", "num": n, "safe": "active"})
    req = urllib.request.Request(GOOGLE_CSE_URL + "?" + params, headers={"User-Agent": "sutradara-bot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except Exception as e:
        log.warning("google_image_search gagal: %s", e)
        return []
    try:
        items = data.get("items") or []
    except AttributeError:
        items = []
    hasil = []
    for it in items:
        link = str(it.get("link") or "").strip()
        if link and link not in hasil:
            hasil.append(link)
        if len(hasil) >= n:
            break
    return hasil


def validate_catalog_image(url, nama_produk, deskripsi=""):
    """Validasi kandidat gambar (URL) via validator bima vision."""
    if not url:
        return False, "url kosong"
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="gcand_", suffix=".jpg")
        os.close(fd)
        dapat = unduh_gambar(url, tmp)
        if not dapat:
            return False, "gagal unduh atau gambar rusak"
        return validasi_kandidat(dapat, nama_produk, deskripsi)
    except Exception as e:
        log.warning("validate_catalog_image gagal: %s", e)
        return False, "error: %s" % e
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _kandidat_dari_google(nama, deskripsi, maks):
    """Kumpulkan maks kandidat VALID dari Google Custom Search."""
    api_key, cx = _google_key(), _google_cx()
    hasil, sudah = [], set()
    queries = [nama]
    if deskripsi:
        queries.append(("%s %s" % (nama, deskripsi))[:120])
    for q in queries:
        for url in google_image_search(q, api_key, cx, num=5):
            if url in sudah:
                continue
            sudah.add(url)
            cocok, alasan = validate_catalog_image(url, nama, deskripsi)
            if cocok:
                domain = urllib.parse.urlparse(url).netloc
                hasil.append({"img": url, "source": url, "title": q, "domain": domain})
                if len(hasil) >= maks:
                    break
            else:
                log.info("kandidat google ditolak: %s (%s)", url[:80], alasan)
        if len(hasil) >= maks:
            break
    return hasil


def _kandidat_dari_tinyfish(nama, deskripsi, maks):
    """Kumpulkan maks kandidat VALID dari TinyFish (fallback)."""
    key = _tinyfish_key()
    queries = [nama, nama + " shopee", nama + " tokopedia"]
    hasil, sudah = [], set()
    for title in queries:
        res = _search(title) or []
        urls = [(r.get("title"), r.get("url")) for r in res if r.get("url")]
        urls = urls[:6]
        if not urls:
            continue
        results, _ = tinyfish_fetch([u for _, u in urls])
        by_url = {r.get("url"): r for r in results}
        for t2, u in urls:
            r = by_url.get(u)
            if not r:
                for rr in results:
                    if rr.get("final_url") == u:
                        r = rr
                        break
            if not r:
                continue
            src = r.get("final_url") or u
            domain = urllib.parse.urlparse(src).netloc
            for img in gambar_dari_hasil(r):
                if img in sudah:
                    continue
                sudah.add(img)
                cocok, alasan = validate_catalog_image(img, nama, deskripsi)
                if cocok:
                    hasil.append({"img": img, "source": src, "title": r.get("title") or t2, "domain": domain})
                    if len(hasil) >= maks:
                        break
                else:
                    log.info("kandidat tinyfish ditolak: %s (%s)", img[:80], alasan)
            if len(hasil) >= maks:
                break
        if len(hasil) >= maks:
            break
    return hasil


def cari_kandidat_katalog(nama: str, deskripsi: str = "", maks_url: int = 6):
    """Cari kandidat gambar katalog web: Google dulu, fallback TinyFish."""
    maks = max(1, min(int(maks_url or 5), 5))
    if _google_key() and _google_cx():
        g = _kandidat_dari_google(nama, deskripsi, maks)
        if g:
            log.info("kandidat katalog dari google: %d", len(g))
            return g
        log.info("google tidak memberi kandidat valid, fallback tinyfish")
    t = _kandidat_dari_tinyfish(nama, deskripsi, maks)
    if t:
        log.info("kandidat katalog dari tinyfish: %d", len(t))
    return t


SYSTEM_BEDAH = """Kamu analis e-commerce Indonesia. Dari isi halaman web berikut, ekstrak info produknya.

ATURAN:
- Kalau ini halaman PRODUK (ada nama produk / harga / deskripsi), ekstrak seakurat mungkin.
- Kalau BUKAN halaman produk (artikel, homepage, halaman error/login, captcha), balas {"error": "jelaskan singkat kenapa"}.
- image_url: pilih SATU URL gambar produk utama dari DAFTAR GAMBAR yang diberikan — yang paling terlihat seperti foto produk asli (bukan logo/banner/ikon).

OUTPUT JSON SAJA, tanpa penjelasan:
{"nama": "nama produk", "deskripsi": "2-3 kalimat: bentuk, bahan, fungsi, keunggulan", "harga": "mis. Rp 59.000 (kosongkan kalau tidak ada)", "image_url": "URL gambar produk utama"}"""


def _fetch_og_direct(url: str):
    """Fallback kalau TinyFish gagal: fetch HTML langsung, ambil OG tags."""
    try:
        req = urllib.request.Request(url, headers={**_UA, "Accept": "text/html"})
        html = urllib.request.urlopen(req, timeout=25).read(600000).decode("utf-8", errors="ignore")
    except Exception:
        return None
    low = html.lower()
    def _meta(prop):
        for q in ('"', "'"):
            key = "property=" + q + "og:" + prop + q
            i = low.find(key)
            if i < 0:
                continue
            tag = html[i:html.find(">", i)]
            ck = "content=" + q
            j = tag.lower().find(ck)
            if j >= 0:
                k = j + len(ck)
                e = tag.find(q, k)
                if e > k:
                    return tag[k:e].strip()
        return ""
    title = _meta("title")
    desc = _meta("description")
    m = _OG_RE.search(html)
    img = (m.group(1).strip() if m else None) or _meta("image")
    if not title and not img:
        return None
    return {"title": title, "description": desc, "image": img, "url": url}


def bedah_link(url: str) -> dict:
    """Fetch satu link produk -> LLM ekstrak {nama, deskripsi, harga, image_url}."""
    results, _ = tinyfish_fetch([url])
    r = results[0] if results else None
    og_fb = None
    if not r:
        og_fb = _fetch_og_direct(url)
        if not og_fb:
            raise RuntimeError("halaman tidak bisa diambil (fetch gagal)")
        r = {"title": og_fb["title"], "description": og_fb["description"], "text": "", "final_url": url}
    teks = r.get("text") or ""
    if isinstance(teks, dict):
        teks = ""
    imgs = [og_fb["image"]] if (og_fb and og_fb.get("image")) else gambar_dari_hasil(r)[:12]
    user = (f"URL: {r.get('final_url') or url}\n"
            f"Judul halaman: {r.get('title', '')}\n"
            f"Deskripsi meta: {r.get('description', '')}\n\n"
            f"ISI HALAMAN (potongan):\n{teks[:4000]}\n\n"
            f"DAFTAR GAMBAR DI HALAMAN:\n"
            + "\n".join(f"- {u}" for u in imgs)
            + "\n\nEkstrak info produknya.")
    raw = _call_omni(SYSTEM_BEDAH, user)
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(m.group(0) if m else raw)
    if data.get("error"):
        raise RuntimeError(data["error"])
    if not data.get("nama"):
        raise RuntimeError("tidak terdeteksi sebagai halaman produk")
    data["image_url"] = data.get("image_url") or (imgs[0] if imgs else "")
    data["source"] = r.get("final_url") or url
    return data


def unduh_gambar(url: str, tujuan: str) -> str:
    """Unduh gambar + verifikasi valid. Return path tujuan."""
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(15_000_000)
    if len(data) < 3000:
        raise RuntimeError("file gambar terlalu kecil / bukan gambar")
    from PIL import Image
    Image.open(io.BytesIO(data)).verify()
    with open(tujuan, "wb") as f:
        f.write(data)
    return tujuan


# ---------------------------------------------------------------------------
# VALIDASI OTOMATIS KATALOG (AI VISION)
# Tiap kandidat gambar dari web divalidasi bima vision SEBELUM ditampilkan
# ke user. Fail-open: vision gagal/timeout -> anggap cocok (jangan blokir alur).
# ---------------------------------------------------------------------------
_OMNI_BASE = os.environ.get("OMNI_BASE", "http://192.168.1.7:20128/v1")
_OMNI_KEY = os.environ.get("OMNI_API_KEY", "")
_OMNI_MODEL = os.environ.get("OMNI_TEXT_MODEL", "bima")
_VALIDASI_TIMEOUT = 30  # detik


def _ke_ascii(teks) -> str:
    """Paksa string jadi ASCII murni (buang aksen/emoji)."""
    import unicodedata
    return unicodedata.normalize("NFKD", str(teks or "")).encode(
        "ascii", "ignore").decode("ascii")


def _mime_gambar(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _panggil_vision(path_gambar: str, prompt: str) -> str:
    """Kirim gambar + prompt ke bima vision via OMNI /chat/completions.
    Mengembalikan teks jawaban. Timeout 30 detik."""
    with open(path_gambar, "rb") as f:
        data = f.read()
    data_url = "data:%s;base64,%s" % (
        _mime_gambar(data), base64.b64encode(data).decode("ascii"))
    body = json.dumps({
        "model": _OMNI_MODEL,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
        "temperature": 0,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if _OMNI_KEY:
        headers["Authorization"] = "Bearer " + _OMNI_KEY
    req = urllib.request.Request(
        _OMNI_BASE + "/chat/completions", data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=_VALIDASI_TIMEOUT) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw)["choices"][0]["message"]["content"]


def _artikan_cocok(obj: dict):
    """Ambil (cocok, alasan) dari jawaban JSON vision."""
    cocok = obj.get("cocok", True)
    if isinstance(cocok, str):
        cocok = cocok.strip().lower() in ("true", "1", "ya", "yes")
    return bool(cocok), _ke_ascii(obj.get("alasan", ""))[:200]


def validasi_kandidat(path_gambar: str, nama_produk: str,
                      deskripsi: str = "") -> tuple:
    """Validasi kandidat katalog dgn AI vision SEBELUM ditampilkan ke user.

    Mengembalikan (cocok: bool, alasan: str). Alasan hanya untuk log/debug,
    JANGAN ditampilkan ke user.
    FAIL-OPEN: jika vision gagal/timeout/tidak bisa diparse -> (True, alasan),
    supaya alur tidak diblokir hanya karena validator mati.
    """
    prompt = (
        "Gambar ini adalah kandidat katalog untuk produk: %s (%s). "
        "Jawab hanya JSON: {\"cocok\": true/false, \"alasan\": \"singkat\"}. "
        "Kriteria: cocok=true HANYA jika gambar jelas menampilkan produk yang "
        "dimaksud (bukan manusia, bukan logo saja, bukan gambar irrelevan, "
        "bukan placeholder/tracking pixel)."
    ) % (_ke_ascii(nama_produk), _ke_ascii(deskripsi))
    try:
        jawaban = _panggil_vision(path_gambar, prompt)
        m = re.search(r"\{.*\}", jawaban, re.S)
        cocok, alasan = _artikan_cocok(json.loads(m.group(0)) if m else {})
        if not cocok:
            log.warning("validator: kandidat DITOLAK: %s", alasan)
        return cocok, alasan
    except Exception as e:  # timeout / jaringan / parse -> fail-open
        log.warning("validator vision gagal -> fail-open cocok=true: %s", e)
        return True, _ke_ascii("validator-gagal: %s" % e)[:200]
