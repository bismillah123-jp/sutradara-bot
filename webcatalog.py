"""Cari katalog mirip di web + bedah link produk e-commerce Indonesia.
Mendukung Shopee, Tokopedia, TikTok Shop, Lazada, Blibli, dan website apa saja.
Pipeline:
1. URL canonicalization & redirect unshortening (s.shopee.co.id, vt.tiktok.com, dll)
2. Schema.org JSON-LD + OpenGraph + Twitter meta tags extraction
3. TinyFish headless browser rendering fallback
4. Direct slug-to-title extraction fallback jika diblokir anti-bot
5. AI synthesis via Omni (Bima) untuk membersihkan nama, ringkasan deskripsi, harga, dan selling points
6. High-res image cleaner (Shopee _tn to master, Tokopedia 900px, TikTok CDN, Lazada)
7. Multi-tier catalog image search: Google Custom Search -> TinyFish -> Web Marketplace Discovery
8. Fail-open AI Vision validation
"""
import base64
import http.cookiejar
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
_DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/124.0.0.0 Safari/537.36")
_UA = {"User-Agent": _DEFAULT_UA}

_OG_RE = re.compile(
    r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)',
    re.I)
_TW_RE = re.compile(
    r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)',
    re.I)
_JUNK = ("logo", "icon", "sprite", "banner", "placeholder", "avatar",
         "favicon", "pixel", "badge", "loading", "spinner", "qr", "track")


def extract_first_url(text: str) -> str:
    """Ekstrak URL pertama dari teks (bahkan jika ada teks promo/caption di sekelilingnya)."""
    if not text:
        return ""
    # Cocokkan http:// atau https://
    m = re.search(r'https?://[^\s<>"]+', text)
    if m:
        return m.group(0).rstrip('.,;:!?)>"\'')
    # Cek format domain tanpa protokol jika ada (seperti s.shopee.co.id/xxx atau vt.tiktok.com/xxx)
    m2 = re.search(
        r'(?:(?:shope\.ee|tokopedia\.link|bit\.ly|tinyurl\.com)|'
        r'(?:[a-zA-Z0-9-]+\.)+(?:shopee\.co\.id|tiktok\.com|tokopedia\.com|lazada\.co\.id|blibli\.com))/[^\s<>"]+',
        text, re.I)
    if m2:
        return "https://" + m2.group(0).rstrip('.,;:!?)>"\'')
    return ""


def clean_ecommerce_image_url(url: str) -> str:
    """Upgrade URL thumbnail marketplace menjadi gambar master beresolusi tinggi."""
    if not url:
        return ""
    u = url.strip()
    if u.startswith("//"):
        u = "https:" + u
    # Shopee & Shopee CDN (down-id.img.susercontent.com)
    if any(k in u for k in ("shopee", "susercontent.com")):
        u = re.sub(r'(_tn|\.[0-9]+x[0-9]+)(\.(jpg|png|webp|jpeg))?$', r'\2', u, flags=re.I)
        u = re.sub(r'(_tn|\.[0-9]+x[0-9]+)\.(jpg|png|webp|jpeg)', r'.\2', u, flags=re.I)
        u = re.sub(r'@resize\([^)]+\)', '', u)
        u = re.sub(r'@[a-zA-Z0-9_-]+', '', u)
    # Tokopedia CDN (images.tokopedia.net)
    elif "tokopedia.net" in u:
        u = re.sub(r'/img/cache/[0-9]+-(?:square|etc)/', '/img/cache/900/', u)
        u = re.sub(r'/img/cache/[0-9]+/', '/img/cache/900/', u)
    # TikTok Shop CDN (ibyteimg.com, tiktokcdn.com, byteoversea.com)
    elif any(k in u for k in ("ibyteimg.com", "tiktokcdn.com", "byteoversea.com")):
        u = re.sub(r'~tplv-[^?]+', '', u)
        u = re.sub(r'\?[^#]*', '', u)
    # Lazada / Alibaba CDN (alicdn.com, slatic.net)
    elif any(k in u for k in ("lazada", "alicdn", "slatic.net")):
        u = re.sub(r'_[0-9]+x[0-9]+(?:q[0-9]+)?\.(jpg|png|webp|jpeg)', r'.\1', u, flags=re.I)
    # Blibli CDN (static-src.com)
    elif "static-src.com" in u or "blibli.com" in u:
        u = re.sub(r'/w_[0-9]+(?:,h_[0-9]+)?/', '/w_1000/', u)
    return u


def slug_to_title(url: str) -> str:
    """Ekstrak nama produk dari URL slug jika HTML diblokir/kosong."""
    try:
        path = urllib.parse.urlparse(url).path
        segments = [s for s in path.split("/") if s]
        for seg in reversed(segments):
            cleaned = urllib.parse.unquote(seg)
            cleaned = re.sub(r'([-\.][ip][\.-]\d+.*)$', '', cleaned)
            cleaned = re.sub(r'\.(html|htm|php)$', '', cleaned)
            cleaned = re.sub(r'[-_]+', ' ', cleaned).strip()
            words = cleaned.split()
            if len(words) >= 2 and not any(kw in cleaned.lower() for kw in (
                    'product', 'products', 'view', 'item', 'catalog', 'category', 'shop', 'detail')):
                return cleaned.title()
    except Exception:
        pass
    return ""


def resolve_canonical_url(url: str, timeout: int = 12) -> str:
    """Ikuti redirect shortlink (s.shopee.co.id, vt.tiktok.com, tokopedia.link, bit.ly) ke URL asli."""
    if not url:
        return ""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPRedirectHandler
    )
    headers = {
        "User-Agent": _DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.geturl()
    except urllib.error.HTTPError as e:
        return e.url or url
    except Exception:
        return url


def tinyfish_fetch(urls: list[str], want_images: bool = True):
    """Fetch 1-10 URL paralel via TinyFish. Return (results, errors).
    Fail-safe: tidak melempar exception jika key tidak ada atau request gagal."""
    key = _tinyfish_key()
    if not key:
        return [], ["TinyFish API key tidak ada"]
    try:
        body = json.dumps({"urls": urls[:10], "format": "html",
                           "image_links": want_images, "ttl": 0}).encode()
        req = urllib.request.Request(
            FETCH_URL, data=body,
            headers={"X-API-Key": key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=40) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return d.get("results") or [], d.get("errors") or []
    except Exception as e:
        log.warning("tinyfish_fetch error: %s", e)
        return [], [str(e)]


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
    """Ambil URL gambar produk dari satu hasil fetch (og:image + image_links),
    dibersihkan dan diupgrade ke resolusi HD."""
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
    for raw_u in out:
        clean_u = clean_ecommerce_image_url(raw_u)
        if clean_u and clean_u not in seen:
            seen.add(clean_u)
            final.append(clean_u)
    return final


# ---------------------------------------------------------------------------
# Ekstraksi Metadata E-Commerce (Schema.org JSON-LD + OpenGraph + Meta Tags)
# ---------------------------------------------------------------------------

def extract_ld_json(html: str) -> dict:
    """Ekstrak data Schema.org Product dari <script type='application/ld+json'>."""
    out = {"images": []}
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            raw_json = m.group(1).strip()
            data = json.loads(raw_json)
            items = data if isinstance(data, list) else [data]
            for it in items:
                candidates = it.get("@graph", []) if isinstance(it.get("@graph"), list) else [it]
                for c in candidates:
                    if isinstance(c, dict) and str(c.get("@type", "")).lower() in ("product", "individualproduct", "itempage"):
                        if c.get("name") and not out.get("name"):
                            out["name"] = str(c["name"]).strip()
                        if c.get("description") and not out.get("description"):
                            out["description"] = str(c["description"]).strip()
                        imgs = c.get("image")
                        if isinstance(imgs, str):
                            out["images"].append(clean_ecommerce_image_url(imgs))
                        elif isinstance(imgs, list):
                            for im in imgs:
                                u = im if isinstance(im, str) else (im.get("url") if isinstance(im, dict) else "")
                                if u:
                                    out["images"].append(clean_ecommerce_image_url(u))
                        offers = c.get("offers")
                        if isinstance(offers, dict):
                            p = offers.get("price")
                            cur = offers.get("priceCurrency", "Rp")
                            if p and not out.get("price"):
                                out["price"] = f"{cur} {p}".strip()
                        elif isinstance(offers, list) and offers:
                            p = offers[0].get("price")
                            cur = offers[0].get("priceCurrency", "Rp")
                            if p and not out.get("price"):
                                out["price"] = f"{cur} {p}".strip()
                        rating = c.get("aggregateRating")
                        if isinstance(rating, dict):
                            out["rating"] = rating.get("ratingValue")
                            out["review_count"] = rating.get("reviewCount")
        except Exception:
            continue
    return out


def extract_product_metadata(html: str, base_url: str = "") -> dict:
    """Ekstraksi terpadu Schema.org JSON-LD + OpenGraph + Twitter meta tags."""
    res = {"nama": "", "deskripsi": "", "harga": "", "images": [], "source": base_url}
    
    # 1. Schema.org JSON-LD
    ld = extract_ld_json(html)
    if ld.get("name"):
        res["nama"] = ld["name"]
    if ld.get("description"):
        res["deskripsi"] = ld["description"]
    if ld.get("price"):
        res["harga"] = ld["price"]
    for im in ld.get("images", []):
        if im and im not in res["images"]:
            res["images"].append(im)

    # 2. OpenGraph / Twitter meta tags helper
    def _meta_content(prop_names):
        for prop in prop_names:
            pat = r'<meta[^>]+(?:property|name)=["\']' + re.escape(prop) + r'["\'][^>]+content=["\']([^"\']+)["\']'
            m = re.search(pat, html, re.I)
            if not m:
                pat2 = r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']' + re.escape(prop) + r'["\']'
                m = re.search(pat2, html, re.I)
            if m and m.group(1).strip():
                return m.group(1).strip()
        return ""

    if not res["nama"]:
        res["nama"] = _meta_content(["og:title", "twitter:title"])
    if not res["nama"]:
        m_t = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
        if m_t:
            t = m_t.group(1).strip()
            # Buang postfix toko seperti '| Tokopedia', '- Shopee'
            t = re.sub(r'\s*([|\-–—]|on\s|di\s).*?(Shopee|Tokopedia|TikTok|Lazada|Blibli).*$', '', t, flags=re.I).strip()
            res["nama"] = t

    if not res["deskripsi"]:
        res["deskripsi"] = _meta_content(["og:description", "twitter:description", "description"])

    og_img = _meta_content(["og:image:secure_url", "og:image", "twitter:image:src", "twitter:image"])
    if og_img:
        clean_og = clean_ecommerce_image_url(_abs_url(og_img, base_url))
        if clean_og and clean_og not in res["images"]:
            res["images"].insert(0, clean_og)

    if not res["harga"]:
        price_val = _meta_content(["product:price:amount", "og:price:amount"])
        price_cur = _meta_content(["product:price:currency", "og:price:currency"]) or "Rp"
        if price_val:
            res["harga"] = f"{price_cur} {price_val}".strip()

    # Ekstrak gambar <img> produk tambahan
    for m_img in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.I):
        u_raw = m_img.group(1).strip()
        if any(j in u_raw.lower() for j in _JUNK):
            continue
        clean_u = clean_ecommerce_image_url(_abs_url(u_raw, base_url))
        if clean_u and clean_u.startswith("http") and clean_u not in res["images"]:
            res["images"].append(clean_u)
            if len(res["images"]) >= 10:
                break

    return res


def _fetch_direct(url: str, timeout: int = 25) -> tuple[str, str]:
    """Fetch HTML langsung dengan realistic headers dan CookieJar. Return (html, final_url)."""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPRedirectHandler
    )
    headers = {
        "User-Agent": _DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.google.com/",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            data = resp.read(1_500_000)
            return data.decode("utf-8", errors="ignore"), resp.geturl()
    except urllib.error.HTTPError as e:
        return "", (e.url or url)
    except Exception as e:
        log.warning("_fetch_direct gagal untuk %s: %s", url, e)
        return "", url


# ---------------------------------------------------------------------------
# Google Custom Search & Web Marketplace Search
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
    items = data.get("items") or []
    hasil = []
    for it in items:
        link = clean_ecommerce_image_url(str(it.get("link") or "").strip())
        if link and link not in hasil and not any(j in link.lower() for j in _JUNK):
            hasil.append(link)
        if len(hasil) >= n:
            break
    return hasil


def _search_ddg_marketplace_urls(query: str, max_results: int = 5) -> list[str]:
    """Cari link produk di Shopee / Tokopedia / Blibli via DuckDuckGo HTML."""
    q = urllib.parse.quote(f"{query} shopee tokopedia")
    url = f"https://html.duckduckgo.com/html/?q={q}"
    req = urllib.request.Request(url, headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        urls = []
        for m in re.finditer(r'<a[^>]+class=["\']result__url["\'][^>]+href=["\']([^"\']+)["\']', html):
            raw_u = m.group(1).strip()
            m_target = re.search(r'uddg=([^&]+)', raw_u)
            if m_target:
                real_u = urllib.parse.unquote(m_target.group(1))
                if real_u.startswith("http") and any(d in real_u for d in ("shopee.co.id", "tokopedia.com", "tiktok.com", "lazada.co.id", "blibli.com")):
                    urls.append(real_u)
        return urls[:max_results]
    except Exception as e:
        log.warning("_search_ddg_marketplace_urls gagal: %s", e)
        return []


def _kandidat_dari_google(nama: str, deskripsi: str, maks: int) -> list[dict]:
    """Kumpulkan kandidat dari Google Custom Search Image."""
    api_key, cx = _google_key(), _google_cx()
    hasil, sudah = [], set()
    queries = [nama]
    if deskripsi:
        queries.append(("%s %s" % (nama, deskripsi))[:120])
    for q in queries:
        for url in google_image_search(q, api_key, cx, num=maks):
            if url in sudah:
                continue
            sudah.add(url)
            domain = urllib.parse.urlparse(url).netloc
            hasil.append({"img": url, "source": url, "title": q, "domain": domain})
            if len(hasil) >= maks:
                break
        if len(hasil) >= maks:
            break
    return hasil


def _kandidat_dari_tinyfish(nama: str, deskripsi: str, maks: int) -> list[dict]:
    """Kumpulkan kandidat dari TinyFish search + fetch."""
    if not _tinyfish_key():
        return []
    queries = [nama, nama + " shopee", nama + " tokopedia"]
    hasil, sudah = [], set()
    for title in queries:
        res = _search(title) or []
        urls = [(r.get("title"), r.get("url")) for r in res if r.get("url")]
        urls = urls[:5]
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
            for raw_img in gambar_dari_hasil(r):
                img = clean_ecommerce_image_url(raw_img)
                if not img or img in sudah or any(j in img.lower() for j in _JUNK):
                    continue
                sudah.add(img)
                hasil.append({"img": img, "source": src, "title": r.get("title") or t2, "domain": domain})
                if len(hasil) >= maks:
                    break
            if len(hasil) >= maks:
                break
        if len(hasil) >= maks:
            break
    return hasil


def _kandidat_dari_web_discovery(nama: str, maks: int) -> list[dict]:
    """Fallback publik: Cari halaman produk e-commerce & ambil gambar produk resminya."""
    urls = _search_ddg_marketplace_urls(nama, max_results=4)
    hasil, sudah = [], set()
    for u in urls:
        html, final_u = _fetch_direct(u, timeout=12)
        if not html:
            continue
        meta = extract_product_metadata(html, final_u)
        domain = urllib.parse.urlparse(final_u).netloc
        for raw_img in meta.get("images", []):
            img = clean_ecommerce_image_url(raw_img)
            if not img or img in sudah or any(j in img.lower() for j in _JUNK):
                continue
            sudah.add(img)
            hasil.append({"img": img, "source": final_u, "title": meta.get("nama") or nama, "domain": domain})
            if len(hasil) >= maks:
                break
        if len(hasil) >= maks:
            break
    return hasil


def cari_kandidat_katalog(nama: str, deskripsi: str = "", maks_url: int = 6) -> list[dict]:
    """Cari kandidat gambar katalog web:
    1. Google Custom Search (jika dikonfigurasi)
    2. TinyFish Search & Fetch (jika key tersedia)
    3. Web Marketplace Discovery via DDG HTML (fallback tanpa kuota/key)
    """
    maks = max(1, min(int(maks_url or 6), 8))
    
    # 1. Google Custom Search
    if _google_key() and _google_cx():
        g = _kandidat_dari_google(nama, deskripsi, maks)
        if g:
            log.info("kandidat katalog dari google: %d", len(g))
            return g
        log.info("google tidak memberi kandidat, lanjut fallback")

    # 2. TinyFish
    t = _kandidat_dari_tinyfish(nama, deskripsi, maks)
    if t:
        log.info("kandidat katalog dari tinyfish: %d", len(t))
        return t

    # 3. Web Marketplace Discovery
    w = _kandidat_dari_web_discovery(nama, maks)
    if w:
        log.info("kandidat katalog dari web discovery: %d", len(w))
        return w

    return []


# ---------------------------------------------------------------------------
# BEDAH LINK PRODUK (ANALISIS DEEP DENGAN SCHEMA.ORG + LLM)
# ---------------------------------------------------------------------------

SYSTEM_BEDAH = """Kamu adalah analis e-commerce profesional Indonesia. Tugasmu mengekstrak dan menyempurnakan informasi produk dari halaman web/marketplace menjadi format terstruktur yang siap dipakai untuk video iklan/promosi.

ATURAN:
1. nama: Nama produk yang bersih, jelas, dan profesional (buang kata-kata spam seperti 'READY STOCK', 'TERMURAH', 'ORIGINAL 100%', 'PROMO CUCI GUDANG', 'GARANSI RESMI', dll).
2. deskripsi: 2-3 kalimat padat merangkum bentuk/desain fisik, bahan/material, fungsi utama, dan cara pemakaian.
3. harga: Format rupiah rapi, misal 'Rp 149.000' (kosongkan string jika tidak ada harga).
4. selling_points: List 3-5 poin keunggulan utama produk dalam array string singkat.
5. image_url: Pilih SATU URL gambar produk utama terbaik dari DAFTAR GAMBAR yang diberikan — yang paling jelas menampilkan produk utama (bukan logo, bukan banner promo, bukan voucher).

OUTPUT HANYA JSON VALID:
{
  "nama": "...",
  "deskripsi": "...",
  "harga": "...",
  "selling_points": ["keunggulan 1", "keunggulan 2", "keunggulan 3"],
  "image_url": "..."
}"""

SYSTEM_BEDAH_SLUG = """Kamu analis e-commerce Indonesia. Bot kami mendeteksi produk dari tautan marketplace, namun halaman detail diblokir anti-bot/captcha.
Berdasarkan nama produk dan URL yang diberikan, susun ringkasan info produk yang akurat, profesional, dan realistis untuk video promosi.

OUTPUT HANYA JSON VALID:
{
  "nama": "nama produk bersih",
  "deskripsi": "2-3 kalimat padat merangkum bentuk/desain fisik, bahan/material, fungsi utama, dan cara pemakaian",
  "harga": "",
  "selling_points": ["keunggulan 1", "keunggulan 2", "keunggulan 3"],
  "image_url": ""
}"""


def bedah_link(url: str) -> dict:
    """Fetch & bedah satu link produk -> ekstrak {nama, deskripsi, harga, selling_points, image_url, images, source}."""
    clean_in_url = extract_first_url(url) or url.strip()
    canonical_url = resolve_canonical_url(clean_in_url)
    slug_name = slug_to_title(canonical_url) or slug_to_title(clean_in_url)
    
    html = ""
    imgs = []
    meta = {}
    title = slug_name
    desc = ""

    # 1. Coba TinyFish Fetch terlebih dahulu jika ada key
    if _tinyfish_key():
        try:
            results, _ = tinyfish_fetch([canonical_url], want_images=True)
            if results:
                r = results[0]
                html = r.get("text") if isinstance(r.get("text"), str) else ""
                title = r.get("title") or title
                desc = r.get("description") or ""
                imgs.extend(gambar_dari_hasil(r))
        except Exception as e:
            log.warning("Tinyfish bedah_link gagal: %s", e)

    # 2. Jika HTML masih kosong, fetch langsung (Direct HTTP)
    if not html:
        direct_html, final_u = _fetch_direct(canonical_url)
        if direct_html:
            html = direct_html
            canonical_url = final_u or canonical_url

    # 3. Ekstraksi Schema.org & Meta Tags jika ada HTML
    if html:
        meta = extract_product_metadata(html, canonical_url)
        if meta.get("nama"):
            title = meta["nama"]
        if meta.get("deskripsi"):
            desc = meta["deskripsi"]
        for im in meta.get("images", []):
            if im and im not in imgs:
                imgs.append(im)

    # 4. Jika HTML didapat atau kita punya teks/meta -> Panggil LLM Bima untuk synthesise
    if html or meta.get("nama") or title:
        user_prompt = (
            f"URL: {canonical_url}\n"
            f"Judul terdeteksi: {title}\n"
            f"Deskripsi meta: {desc}\n"
            + (f"Harga terdeteksi: {meta.get('harga')}\n" if meta.get("harga") else "")
            + f"\nISI HALAMAN / KONTEN (potongan):\n{html[:3500]}\n\n"
            f"DAFTAR GAMBAR DI HALAMAN:\n"
            + "\n".join(f"- {u}" for u in imgs[:15])
            + "\n\nEkstrak dan rapikan info produknya."
        )
        try:
            raw = _call_omni(SYSTEM_BEDAH, user_prompt)
            m = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(m.group(0) if m else raw)
        except Exception as e:
            log.warning("LLM bedah_link gagal parse: %s", e)
            data = {}

        nama_hasil = data.get("nama") or title or slug_name
        if not nama_hasil:
            raise RuntimeError("tidak terdeteksi sebagai halaman produk")

        # Prioritaskan image_url dari LLM jika ada di imgs, atau ambil gambar pertama
        chosen_img = data.get("image_url") or (imgs[0] if imgs else "")
        if chosen_img and chosen_img not in imgs:
            imgs.insert(0, chosen_img)

        # Fallback harga dari meta jika LLM kosong
        harga_hasil = data.get("harga") or meta.get("harga", "")

        return {
            "nama": nama_hasil,
            "deskripsi": data.get("deskripsi") or desc or f"Produk {nama_hasil} berkualitas tinggi.",
            "harga": harga_hasil,
            "selling_points": data.get("selling_points", []),
            "image_url": chosen_img,
            "images": imgs,
            "source": canonical_url,
        }

    # 5. Fallback ketika anti-bot memblokir halaman sepenuhnya tapi URL memiliki slug nama produk
    if slug_name:
        log.info("Anti-bot memblokir direct fetch, fallback ke slug recovery: %s", slug_name)
        user_prompt = f"URL Produk: {canonical_url}\nNama Produk Slug: {slug_name}"
        try:
            raw = _call_omni(SYSTEM_BEDAH_SLUG, user_prompt)
            m = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(m.group(0) if m else raw)
        except Exception:
            data = {
                "nama": slug_name,
                "deskripsi": f"{slug_name} dengan desain modern, fungsional, dan material berkualitas tinggi.",
                "harga": "",
                "selling_points": ["Kualitas terjamin", "Desain fungsional", "Praktis digunakan"],
            }
        
        # Cari gambar katalog web secara otomatis untuk melengkapi gambar yang diblokir
        cands = cari_kandidat_katalog(slug_name, data.get("deskripsi", ""), maks_url=4)
        cand_imgs = [c["img"] for c in cands if c.get("img")]

        return {
            "nama": data.get("nama") or slug_name,
            "deskripsi": data.get("deskripsi", ""),
            "harga": data.get("harga", ""),
            "selling_points": data.get("selling_points", []),
            "image_url": cand_imgs[0] if cand_imgs else "",
            "images": cand_imgs,
            "source": canonical_url,
        }

    raise RuntimeError("Halaman produk tidak dapat diakses atau URL tidak valid")


# ---------------------------------------------------------------------------
# UNDUH GAMBAR DENGAN VALIDASI & STANDARISASI RGB JPEG
# ---------------------------------------------------------------------------

def unduh_gambar(url: str, tujuan: str) -> str:
    """Unduh gambar dari URL, bersihkan, standarisasi ke RGB JPEG berkualitas tinggi,
    dan simpan ke path tujuan. Mencegah error Telegram invalid photo."""
    if not url:
        raise ValueError("URL gambar kosong")
    url = clean_ecommerce_image_url(url)
    parsed = urllib.parse.urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    headers = {
        "User-Agent": _DEFAULT_UA,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": origin + "/",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(15_000_000)
    if len(data) < 2500:
        raise RuntimeError("file gambar terlalu kecil / bukan gambar")
    
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    
    # Standarisasi format: konversi RGBA / Palette / LA ke RGB solid (background putih)
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        mask = img.split()[-1] if "A" in img.mode else None
        bg.paste(img, mask=mask)
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    # Rescale jika terlalu besar (>2500px) agar cepat & efisien
    if max(img.size) > 2500:
        img.thumbnail((2500, 2500), Image.Resampling.LANCZOS)

    # Simpan sebagai clean RGB JPEG
    os.makedirs(os.path.dirname(os.path.abspath(tujuan)), exist_ok=True)
    img.save(tujuan, format="JPEG", quality=95, optimize=True)
    return tujuan


# ---------------------------------------------------------------------------
# VALIDASI OTOMATIS KATALOG (AI VISION)
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
    """Kirim gambar + prompt ke bima vision via OMNI /chat/completions."""
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
    FAIL-OPEN: jika vision gagal/timeout/tidak bisa diparse -> (True, alasan)."""
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


def validate_catalog_image(url: str, nama_produk: str, deskripsi: str = "") -> tuple:
    """Helper kompatibilitas unduh + validasi vision."""
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
