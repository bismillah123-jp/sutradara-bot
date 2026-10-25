"""Cari katalog mirip di web + bedah link produk.
Pakai TinyFish Fetch (POST https://api.fetch.tinyfish.ai, GRATIS) + bima."""
import io
import json
import re
import urllib.parse
import urllib.request

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
def cari_kandidat_katalog(nama: str, deskripsi: str = "",
                          maks_url: int = 6) -> list[dict]:
    """Cari halaman produk di web -> ekstrak gambarnya.
    Return [{img, source, title, domain}]."""
    queries = [nama, f"{nama} shopee", f"{nama} tokopedia"]
    seen, urls = set(), []
    for q in queries:
        try:
            # recency dimatikan (maks): yang penting gambarnya cocok
            for it in _search(q.strip(), limit=6, recency_minutes=5256000):
                u = it.get("url", "")
                if u and u not in seen and not u.lower().endswith(".pdf"):
                    seen.add(u)
                    urls.append((it.get("title", ""), u))
        except Exception:
            continue
        if len(urls) >= maks_url:
            break
    urls = urls[:maks_url]
    if not urls:
        return []
    results, _ = tinyfish_fetch([u for _, u in urls])
    by_url = {r.get("url"): r for r in results}
    cands = []
    for title, u in urls:
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
        for img in gambar_dari_hasil(r)[:2]:
            cands.append({"img": img, "source": src,
                          "title": r.get("title") or title,
                          "domain": domain})
    return cands

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
    imgs = [og_fb["image"]] if og_fb and og_fb["image"] else gambar_dari_hasil(r)[:12]
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
