"""Riset produk viral - TinyFish Search (data web asli) + sintesis LLM bima.
Fallback: LLM murni kalau TinyFish gagal / tidak ada key."""
import json
import os
import re
import urllib.parse
import urllib.request

from storyboard import _call_omni

KEY_FILES = ("/home/ihsan/.tinyfish/config.json",
             "/home/ihsan/.config/tinyfish/config.json")
SEARCH_URL = "https://api.search.tinyfish.ai/"


def _tinyfish_key() -> str:
    k = os.environ.get("TINYFISH_API_KEY", "")
    if k:
        return k
    for p in KEY_FILES:
        try:
            with open(p) as fh:
                d = json.load(fh)
            for f in ("api_key", "apiKey"):
                if d.get(f):
                    return d[f]
        except Exception:
            continue
    return ""


def _search(query: str, limit: int = 8,
            recency_minutes: int = 43200) -> list[dict]:
    """recency_minutes=43200 -> batasi hasil 30 hari terakhir (hasil paling baru)."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 8
    key = _tinyfish_key()
    if not key:
        return []
    qs = urllib.parse.urlencode({"query": query, "location": "ID",
                                 "language": "id",
                                 "recency_minutes": recency_minutes})
    req = urllib.request.Request(f"{SEARCH_URL}?{qs}",
                                 headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    out = []
    for it in (d.get("results") or [])[:limit]:
        out.append({"title": it.get("title", ""), "url": it.get("url", ""),
                    "snippet": it.get("snippet", ""),
                    "site": it.get("site_name", ""),
                    "date": it.get("published_date", "")})
    return out


def _kumpulkan_data(kategori: str) -> list[dict]:
    kat = kategori.strip()
    queries = [
        f"produk terlaris TikTok Shop Indonesia {kat}".strip(),
        f"produk viral paling laris Shopee Indonesia {kat}".strip(),
        f"produk trending TikTok affiliate Indonesia {kat}".strip(),
    ]
    seen, hasil = set(), []
    for q in queries:
        try:
            for it in _search(q):
                if it["url"] and it["url"] not in seen:
                    seen.add(it["url"])
                    hasil.append(it)
        except Exception:
            continue
    # urutkan paling baru dulu (tanggal kosong taruh paling belakang)
    hasil.sort(key=lambda d: d.get("date") or "", reverse=True)
    return hasil[:16]


SYSTEM_RISET = """Kamu adalah analis tren e-commerce & TikTok Shop Indonesia.

Tugas: pilih produk yang PALING LARIS, paling diminati, dan berpotensi viral/FYP untuk video affiliate, DARI DATA HASIL PENCARIAN WEB yang diberikan user. Produk yang kamu pilih HARUS benar-benar muncul di data pencarian - JANGAN mengarang produk yang tidak ada di data. Kalau data kurang spesifik, pilih yang paling mendekati & masuk akal dari data yang ada.

KRITERIA:
- Produk fisik yang gampang didemokan di video pendek (visual, ada efek "wow")
- Harga terjangkau (di bawah Rp 300rb) - gampang checkout impulsif
- Lagi naik daun / banyak dicari di TikTok & Shopee Indonesia
- Bukan produk terlarang, bukan suplemen/obat klaim medis, bukan barang KW

OUTPUT JSON SAJA:
{"kandidat": [
  {"nama": "nama produk spesifik",
   "kategori": "kategori singkat",
   "alasan": "kenapa lagi laris & berpotensi viral (1-2 kalimat, sebut hook visualnya)",
   "harga_kisaran": "Rp xxrb - Rp xxrb",
   "deskripsi": "deskripsi produk 2-3 kalimat: bentuk, bahan, fungsi, keunggulan - cukup detail untuk prompt gambar AI"}
]}"""

SYSTEM_RISET_LLM = """Kamu adalah analis tren e-commerce & TikTok Shop Indonesia.

Tugas: riset produk yang PALING LARIS, paling diminati, dan berpotensi viral/FYP untuk video affiliate.

KRITERIA:
- Produk fisik yang gampang didemokan di video pendek (visual, ada efek "wow")
- Harga terjangkau (di bawah Rp 300rb) - gampang checkout impulsif
- Lagi naik daun / banyak dicari di TikTok & Shopee Indonesia
- Bukan produk terlarang, bukan suplemen/obat klaim medis, bukan barang KW

OUTPUT JSON SAJA:
{"kandidat": [
  {"nama": "nama produk spesifik",
   "kategori": "kategori singkat",
   "alasan": "kenapa lagi laris & berpotensi viral (1-2 kalimat, sebut hook visualnya)",
   "harga_kisaran": "Rp xxrb - Rp xxrb",
   "deskripsi": "deskripsi produk 2-3 kalimat: bentuk, bahan, fungsi, keunggulan - cukup detail untuk prompt gambar AI"}
]}"""


def _parse_kandidat(raw: str) -> list[dict]:
    raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
    raw = re.sub(r"\n?```$", "", raw.strip())
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(m.group(0) if m else raw)
    kandidat = data.get("kandidat", [])
    assert kandidat, "riset tidak menghasilkan kandidat"
    return kandidat[:5]


async def research_products(kategori: str = "") -> list[dict]:
    fokus = (f"Fokus kategori: {kategori}." if kategori
             else "Semua kategori bebas.")
    data = _kumpulkan_data(kategori)
    if data:
        blok = "\n".join(
            f"{i+1}. {d['title']} ({d['site']}, {d['date'] or 'tanpa tanggal'})\n"
            f"   {d['snippet']}\n   {d['url']}"
            for i, d in enumerate(data))
        user = (f"{fokus}\nBerikan TEPAT 5 kandidat produk.\n\n"
                f"DATA HASIL PENCARIAN WEB 30 HARI TERAKHIR "
                f"(diurutkan paling baru dulu):\n{blok}")
        system = SYSTEM_RISET
    else:
        user = f"{fokus}\nBerikan TEPAT 5 kandidat produk."
        system = SYSTEM_RISET_LLM
    last = None
    for _ in range(3):
        try:
            return _parse_kandidat(_call_omni(system, user))
        except Exception as e:
            last = e
    raise last if last else RuntimeError("riset gagal")


SYSTEM_CAPTION = """Kamu copywriter TikTok Affiliate Indonesia yang paham tren 2026.

Tugas: bikin CAPTION + HASHTAG siap posting untuk video affiliate produk berikut, pakai DATA TREN dari hasil pencarian web.

ATURAN CAPTION:
- Bahasa Indonesia santai gaya TikTok (gue/lo)
- Hook kuat di kalimat pertama (bikin penasaran/relate)
- 2-4 kalimat, sebut keunggulan produk
- CTA beli di akhir (mis. "klik keranjang kuning")
- Tanpa klaim medis/berlebihan

ATURAN HASHTAG:
- 8-12 hashtag, diawali #, huruf kecil semua, tanpa spasi
- Campur 2-3 broad viral (#fyp, #racuntiktok) + sisanya niche sesuai produk

OUTPUT JSON SAJA:
{"caption": "teks caption", "hashtags": ["#tag1", "#tag2"]}"""


def riset_caption(produk: str, deskripsi: str = "") -> dict:
    """Riset caption + hashtag dari tren web terkini. Return {caption, hashtags}."""
    queries = [f"caption promosi TikTok affiliate {produk}",
               f"hashtag viral TikTok Shop {produk}"]
    seen, hasil = set(), []
    for q in queries:
        try:
            for it in _search(q.strip(), limit=5):
                if it.get("url") and it["url"] not in seen:
                    seen.add(it["url"])
                    hasil.append(it)
        except Exception:
            continue
    blok = "\n".join(f"- {d['title']}: {d['snippet']}" for d in hasil[:10])
    user = (f"Produk: {produk}\nDeskripsi: {deskripsi}\n\n"
            f"DATA TREN TERKINI:\n{blok or '(tidak ada data tambahan)'}\n\n"
            "Bikin caption + hashtag-nya.")
    last = None
    for _ in range(3):
        try:
            raw = _call_omni(SYSTEM_CAPTION, user)
            m = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(m.group(0) if m else raw)
            assert data.get("caption") and data.get("hashtags")
            tags = data["hashtags"]
            if isinstance(tags, str):
                tags = tags.split()
            data["hashtags"] = [(t if t.startswith("#") else "#" + t)
                                for t in tags][:12]
            return data
        except Exception as e:
            last = e
    raise last if last else RuntimeError("riset caption gagal")
