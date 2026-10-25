"""Sistem billing komersial untuk Sutradara Bot: saldo, voucher, topup."""
import json
import time
import uuid
import os
import urllib.request
import urllib.parse
from pathlib import Path

from admin import DATA_DIR, users_db, _save

HARGA_VIDEO_DEFAULT = 500

KOMERSIAL_AKTIF_DEFAULT = True

# FASE 4: batas generate harian user unlimited (diubah via /setbatas)
BATAS_UNLIMITED_HARIAN = 10

VOUCHERS_FILE = DATA_DIR / "vouchers.json"
TOPUPS_FILE = DATA_DIR / "topups.json"
BILLING_CONFIG_FILE = DATA_DIR / "billing_config.json"

_DEFAULT_PAYMENT_INFO = (
    "Silakan transfer manual ke:\n"
    "Bank BCA 1234567890 a.n. Sir Ihsan\n"
    "\n"
    "Setelah transfer, tekan tombol Sudah Bayar ya."
)


def _load_json(path, default):
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else default
    except Exception:
        return default


def _save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=True, indent=1), encoding="utf-8")
    tmp.replace(path)


def _config():
    return _load_json(BILLING_CONFIG_FILE, {})


def _save_config(cfg):
    _save_json(BILLING_CONFIG_FILE, cfg)


def harga_video():
    try:
        return int(_config().get("harga_video", HARGA_VIDEO_DEFAULT))
    except Exception:
        return HARGA_VIDEO_DEFAULT


def komersial_aktif():
    # Status master switch komersial.
    cfg = _config()
    return bool(cfg.get("komersial_aktif", KOMERSIAL_AKTIF_DEFAULT))


def set_komersial(aktif):
    # Nyalakan/matikan master switch. Tersimpan di billing_config.json.
    cfg = _config()
    cfg["komersial_aktif"] = bool(aktif)
    _save_config(cfg)
    return bool(aktif)


def set_harga(rupiah):
    rupiah = int(rupiah)
    if rupiah < 0:
        raise ValueError("harga negatif")
    cfg = _config()
    cfg["harga_video"] = rupiah
    _save_config(cfg)
    return rupiah


def get_payment_info():
    return _config().get("payment_info", _DEFAULT_PAYMENT_INFO)


def set_payment_info(teks):
    cfg = _config()
    cfg["payment_info"] = teks
    _save_config(cfg)
    return teks


def rp(n):
    return "%s" % f"{int(n):,}".replace(",", ".")


def fmt_sisa(detik):
    detik = int(detik)
    if detik <= 0:
        return "habis"
    hari, detik = divmod(detik, 86400)
    jam, detik = divmod(detik, 3600)
    mnt, _ = divmod(detik, 60)
    if hari:
        return "%d hari %d jam" % (hari, jam)
    if jam:
        return "%d jam %d menit" % (jam, mnt)
    return "%d menit" % mnt


def _user(uid):
    k = str(uid)
    d = users_db.get(k)
    if not isinstance(d, dict):
        d = {}
        users_db[k] = d
    d.setdefault("saldo", 0)
    d.setdefault("unlimited_until", 0)
    return d


def get_saldo(uid):
    try:
        return int(_user(uid).get("saldo", 0))
    except Exception:
        return 0


def tambah_saldo(uid, amount):
    d = _user(uid)
    d["saldo"] = int(d.get("saldo", 0)) + int(amount)
    _save()
    return d["saldo"]


def kurang_saldo(uid, amount):
    if get_saldo(uid) < int(amount):
        return False
    d = _user(uid)
    d["saldo"] = int(d.get("saldo", 0)) - int(amount)
    _save()
    return True


def is_unlimited(uid):
    try:
        return float(_user(uid).get("unlimited_until", 0)) > time.time()
    except Exception:
        return False


def unlimited_sisa(uid):
    try:
        sisa = float(_user(uid).get("unlimited_until", 0)) - time.time()
    except Exception:
        sisa = 0
    return int(sisa) if sisa > 0 else 0


def _tgl_hari_ini():
    return time.strftime("%Y-%m-%d")


def batas_unlimited_harian():
    try:
        return int(_config().get("batas_unlimited_harian", BATAS_UNLIMITED_HARIAN))
    except Exception:
        return BATAS_UNLIMITED_HARIAN


def set_batas_unlimited(angka):
    angka = int(angka)
    if angka < 1:
        raise ValueError("batas minimal 1")
    cfg = _config()
    cfg["batas_unlimited_harian"] = angka
    _save_config(cfg)
    return angka


def unlimited_harian_terpakai(uid):
    d = _user(uid)
    info = d.get("unlimited_harian") or {}
    if info.get("tgl") != _tgl_hari_ini():
        return 0
    try:
        return int(info.get("n", 0))
    except Exception:
        return 0


def cek_unlimited_harian(uid):
    # Return (boleh, pesan). Tolak jika kuota harian unlimited habis.
    if not is_unlimited(uid):
        return True, "ok"
    batas = batas_unlimited_harian()
    if unlimited_harian_terpakai(uid) >= batas:
        return False, "Batas harian unlimited tercapai (%d/hari). Coba lagi besok." % batas
    return True, "ok"


def catat_unlimited_harian(uid):
    # Tambah counter generate harian user unlimited. Reset tiap tengah malam.
    d = _user(uid)
    info = d.get("unlimited_harian") or {}
    if info.get("tgl") != _tgl_hari_ini():
        info = {"tgl": _tgl_hari_ini(), "n": 0}
    try:
        n = int(info.get("n", 0))
    except Exception:
        n = 0
    info["n"] = n + 1
    d["unlimited_harian"] = info
    _save()
    return info["n"]


def get_discount_pct(uid):
    try:
        return int(_user(uid).get("discount_pct", 0))
    except Exception:
        return 0


def clear_discount_pct(uid):
    d = _user(uid)
    if "discount_pct" in d:
        del d["discount_pct"]
        _save()


def harga_untuk(uid):
    h = harga_video()
    pct = get_discount_pct(uid)
    if pct > 0:
        h = h - (h * pct // 100)
    return max(0, h)


def _vouchers():
    return _load_json(VOUCHERS_FILE, {})


def _save_vouchers(v):
    _save_json(VOUCHERS_FILE, v)


def voucher_add(kode, tipe, nilai, max_uses=1, hari_berlaku=30):
    kode = str(kode).strip().upper()
    if not kode:
        raise ValueError("kode kosong")
    tipe = str(tipe).strip().lower()
    if tipe not in ("discount", "credit", "unlimited"):
        raise ValueError("tipe harus discount/credit/unlimited")
    nilai = int(nilai)
    if tipe == "discount" and not 1 <= nilai <= 100:
        raise ValueError("discount 1-100")
    if tipe == "credit" and nilai <= 0:
        raise ValueError("credit harus > 0")
    if tipe == "unlimited" and not 1 <= nilai <= 168:
        raise ValueError("unlimited 1-168 jam")
    max_uses = int(max_uses or 1)
    if max_uses < 1:
        max_uses = 1
    hari = float(hari_berlaku or 0)
    now = time.time()
    v = _vouchers()
    v[kode] = {
        "type": tipe,
        "value": nilai,
        "max_uses": max_uses,
        "used_count": 0,
        "used_by": [],
        "expires_at": (now + hari * 86400) if hari > 0 else 0,
        "active": True,
        "created_at": now,
    }
    _save_vouchers(v)
    return kode


def voucher_list():
    return _vouchers()


def voucher_del(kode):
    kode = str(kode).strip().upper()
    v = _vouchers()
    if kode not in v:
        return False
    v[kode]["active"] = False
    _save_vouchers(v)
    return True


def redeem_voucher(uid, kode):
    kode = str(kode).strip().upper()
    v = _vouchers()
    vc = v.get(kode)
    now = time.time()
    if not vc or not vc.get("active"):
        return False, "Kode voucher tidak dikenal atau sudah nonaktif."
    if vc.get("expires_at", 0) and now > vc["expires_at"]:
        return False, "Voucher sudah kedaluwarsa."
    if int(vc.get("used_count", 0)) >= int(vc.get("max_uses", 1)):
        return False, "Kuota voucher sudah habis."
    if str(uid) in vc.get("used_by", []):
        return False, "Kamu sudah pernah pakai voucher ini."
    tipe = vc.get("type")
    nilai = int(vc.get("value", 0))
    d = _user(uid)
    if tipe == "discount":
        d["discount_pct"] = nilai
        pesan = "Voucher dipakai. Diskon %d persen untuk 1x generate berikutnya." % nilai
    elif tipe == "credit":
        d["saldo"] = int(d.get("saldo", 0)) + nilai
        pesan = "Voucher dipakai. Saldo bertambah Rp %s." % rp(nilai)
    elif tipe == "unlimited":
        base = max(float(d.get("unlimited_until", 0)), now)
        d["unlimited_until"] = base + nilai * 3600
        pesan = "Voucher dipakai. Unlimited aktif %d jam." % nilai
    else:
        return False, "Tipe voucher tidak dikenal."
    vc["used_count"] = int(vc.get("used_count", 0)) + 1
    ub = vc.get("used_by", [])
    ub.append(str(uid))
    vc["used_by"] = ub
    _save_vouchers(v)
    _save()
    return True, pesan


def _topups():
    return _load_json(TOPUPS_FILE, {})


def _save_topups(t):
    _save_json(TOPUPS_FILE, t)


def buat_topup(uid, amount):
    amount = int(amount)
    if amount <= 0:
        raise ValueError("nominal harus > 0")
    tid = uuid.uuid4().hex[:8]
    t = _topups()
    while tid in t:
        tid = uuid.uuid4().hex[:8]
    t[tid] = {
        "uid": str(uid),
        "amount": amount,
        "status": "pending",
        "created_at": time.time(),
    }
    _save_topups(t)
    return tid


def get_topup(tid):
    return _topups().get(str(tid))


def topup_pending():
    t = _topups()
    return {k: x for k, x in t.items() if x.get("status") == "pending"}


def approve_topup(tid):
    tid = str(tid)
    t = _topups()
    x = t.get(tid)
    if not x:
        return False, "Topup tidak ditemukan."
    if x.get("status") != "pending":
        return False, "Topup sudah diproses."
    x["status"] = "approved"
    x["decided_at"] = time.time()
    _save_topups(t)
    saldo = tambah_saldo(x["uid"], int(x["amount"]))
    return True, saldo


def reject_topup(tid):
    tid = str(tid)
    t = _topups()
    x = t.get(tid)
    if not x:
        return False, "Topup tidak ditemukan."
    if x.get("status") != "pending":
        return False, "Topup sudah diproses."
    x["status"] = "rejected"
    x["decided_at"] = time.time()
    _save_topups(t)
    return True, ""

# ==================== FASE 2: REFERAL ====================
BONUS_PENGGUNA_BARU = 2500
BONUS_PENGUNDANG = 500
REFERRAL_FILE = DATA_DIR / "referrals.json"


def _referrals():
    return _load_json(REFERRAL_FILE, {})


def _save_referrals(r):
    _save_json(REFERRAL_FILE, r)


def bonus_baru_efektif():
    try:
        return int(_config().get("bonus_referal_baru", BONUS_PENGGUNA_BARU))
    except Exception:
        return BONUS_PENGGUNA_BARU


def bonus_pengundang_efektif():
    try:
        return int(_config().get("bonus_referal_pengundang", BONUS_PENGUNDANG))
    except Exception:
        return BONUS_PENGUNDANG


def set_bonus_referal(baru, pengundang):
    baru = int(baru)
    pengundang = int(pengundang)
    if baru < 0 or pengundang < 0:
        raise ValueError("bonus negatif")
    cfg = _config()
    cfg["bonus_referal_baru"] = baru
    cfg["bonus_referal_pengundang"] = pengundang
    _save_config(cfg)
    return baru, pengundang


def catat_referal(uid_baru, uid_pengundang):
    try:
        s_baru = str(int(uid_baru))
        s_undang = str(int(uid_pengundang))
    except (ValueError, TypeError):
        return False, "ID pengundang tidak valid."
    if s_baru == s_undang:
        return False, "Tidak bisa pakai link referal sendiri."
    r = _referrals()
    if s_baru in r:
        return False, "Kamu sudah pernah tercatat pakai referal."
    # FASE 4: bonus TIDAK langsung cair; simpan pending, cair saat video pertama sukses.
    r[s_baru] = {"pengundang": s_undang, "status": "pending"}
    _save_referrals(r)
    return True, "ok"


def _pengundang_dari(entri):
    if isinstance(entri, dict):
        return str(entri.get("pengundang", ""))
    return str(entri)


def cairkan_bonus_referal(uid):
    # FASE 4: cairkan bonus referal pending setelah video PERTAMA user sukses.
    # Bonus hangus jika user tidak pernah generate. Return (True/False, pesan).
    try:
        s_baru = str(int(uid))
    except (ValueError, TypeError):
        return False, "uid tidak valid"
    r = _referrals()
    entri = r.get(s_baru)
    if not isinstance(entri, dict) or entri.get("status") != "pending":
        return False, "tidak ada bonus pending"
    d = _user(uid)
    try:
        sudah = int(d.get("total_generate", 0))
    except Exception:
        sudah = 0
    if sudah > 0:
        return False, "bukan video pertama"
    s_undang = _pengundang_dari(entri)
    tambah_saldo(s_baru, bonus_baru_efektif())
    if s_undang:
        tambah_saldo(s_undang, bonus_pengundang_efektif())
    entri["status"] = "cair"
    r[s_baru] = entri
    _save_referrals(r)
    return True, "ok"


def catat_generate_user(uid):
    # FASE 4: hitung video sukses per user (syarat bonus referal).
    d = _user(uid)
    try:
        n = int(d.get("total_generate", 0))
    except Exception:
        n = 0
    d["total_generate"] = n + 1
    _save()
    return d["total_generate"]


def get_link_referal(bot_username, uid):
    return "https://t.me/%s?start=ref_%s" % (bot_username, uid)


def info_referal(uid):
    k = str(uid)
    teman = [nb for nb, np in _referrals().items() if _pengundang_dari(np) == k]
    return len(teman), len(teman) * bonus_pengundang_efektif()

# ==================== FASE 2: HARGA TIER ANTREAN ====================
HARGA_REGULER = 500
HARGA_PRIORITAS = 1000


def harga_reguler():
    try:
        return int(_config().get("harga_reguler", HARGA_REGULER))
    except Exception:
        return HARGA_REGULER


def harga_prioritas():
    try:
        return int(_config().get("harga_prioritas", HARGA_PRIORITAS))
    except Exception:
        return HARGA_PRIORITAS


def set_harga_tier(reguler, prioritas):
    reguler = int(reguler)
    prioritas = int(prioritas)
    if reguler < 0 or prioritas < 0:
        raise ValueError("harga negatif")
    cfg = _config()
    cfg["harga_reguler"] = reguler
    cfg["harga_prioritas"] = prioritas
    _save_config(cfg)
    return reguler, prioritas


def harga_tier_untuk(uid, tier):
    h = harga_prioritas() if str(tier) == "prioritas" else harga_reguler()
    pct = get_discount_pct(uid)
    if pct > 0:
        h = h - (h * pct // 100)
    return max(0, h)

# ================= FASE 3: STATISTIK DASBOR =================
STATS_FILE = DATA_DIR / "stats.json"
FLOW2API_DB_DEFAULT = "/home/ihsan/apps/flow2api/data/flow.db"


def _stats():
    return _load_json(STATS_FILE, {"total_generate": 0})


def _save_stats(s):
    _save_json(STATS_FILE, s)


def catat_generate():
    """Catat 1 video berhasil terkirim (dipanggil worker setelah send_video sukses)."""
    s = _stats()
    s["total_generate"] = int(s.get("total_generate", 0) or 0) + 1
    _save_stats(s)
    return s["total_generate"]


def _mask_email(email):
    try:
        user, _, dom = str(email).partition("@")
        return ((user[:1] + "***@" + dom) if user else "***@" + dom) if dom else "***"
    except Exception:
        return "***"


def flow2api_kredit():
    """Ambil info kredit flow2api lokal.

    Returns dict(total, n_akun, per_akun=[(email_masked, kredit)]) atau None
    jika tidak ada angka valid (jangan ngarang angka).
    """
    base = os.environ.get("FLOW2API_BASE", "http://127.0.0.1:8090").rstrip("/")
    db_path = os.environ.get("FLOW2API_DB", FLOW2API_DB_DEFAULT)
    total, n_akun, per_akun = None, 0, []
    try:
        req = urllib.request.Request(base + "/", headers={"User-Agent": "sutradara-bot"})
        with urllib.request.urlopen(req, timeout=8) as r:
            info = json.load(r)
        n_akun = int(info.get("active_tokens") or info.get("total_tokens") or 0)
    except Exception:
        pass
    try:
        import sqlite3
        uri = "file:%s?mode=ro&immutable=1" % urllib.parse.quote(db_path)
        con = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            rows = con.execute("SELECT email, credits FROM tokens WHERE is_active = 1").fetchall()
        finally:
            con.close()
        for email, credits in rows:
            try:
                c = int(credits or 0)
            except (TypeError, ValueError):
                continue
            per_akun.append((_mask_email(email), c))
        if per_akun:
            total = sum(c for _, c in per_akun)
            n_akun = n_akun or len(per_akun)
    except Exception:
        pass
    if total is None:
        return None
    return {"total": total, "n_akun": n_akun or len(per_akun), "per_akun": per_akun}


def get_statistik():
    """Kumpulkan statistik bisnis untuk /dasbor. Returns dict."""
    users = {k: v for k, v in (users_db or {}).items()
             if not k.startswith("_") and isinstance(v, dict)}
    total_user = len(users)
    st = _stats()
    total_generate = int(st.get("total_generate", 0) or 0)
    revenue_topup = 0
    for t in (_topups() or {}).values():
        try:
            if (t or {}).get("status") == "approved":
                revenue_topup += int(t.get("amount", 0) or 0)
        except (TypeError, ValueError):
            continue
    try:
        harga_rata2 = (int(harga_reguler()) + int(harga_prioritas())) // 2
    except Exception:
        harga_rata2 = 0
    estimasi_generate = total_generate * harga_rata2
    total_saldo = 0
    for u in users.values():
        try:
            total_saldo += int((u or {}).get("saldo", 0) or 0)
        except (TypeError, ValueError):
            continue
    try:
        import antrean
        q = antrean._load() or []
        n_antri = len(q)
        n_prioritas = sum(1 for j in q if (j or {}).get("tier") == "prioritas")
    except Exception:
        n_antri, n_prioritas = 0, 0
    now = time.time()
    voucher_aktif, voucher_redeem = 0, 0
    for vc in (_vouchers() or {}).values():
        vc = vc or {}
        try:
            used = int(vc.get("used_count", 0) or 0)
        except (TypeError, ValueError):
            used = 0
        voucher_redeem += used
        try:
            maxu = int(vc.get("max_uses", 1) or 1)
        except (TypeError, ValueError):
            maxu = 1
        exp = vc.get("expires_at", 0) or 0
        if vc.get("active") and used < maxu and (not exp or exp > now):
            voucher_aktif += 1
    total_ref = len(_referrals() or {})
    try:
        bonus_per_ref = int(bonus_baru_efektif()) + int(bonus_pengundang_efektif())
    except Exception:
        bonus_per_ref = 0
    return {
        "total_user": total_user,
        "total_generate": total_generate,
        "revenue_topup": revenue_topup,
        "estimasi_generate": estimasi_generate,
        "revenue_total": revenue_topup + estimasi_generate,
        "total_saldo": total_saldo,
        "antri": n_antri,
        "antri_prioritas": n_prioritas,
        "voucher_aktif": voucher_aktif,
        "voucher_redeem": voucher_redeem,
        "ref_sukses": total_ref,
        "ref_bonus": total_ref * bonus_per_ref,
    }

# ================= FASE 5: PANEL SETTINGS ADMIN =================
# Setting tambahan yang bisa diubah admin via command. Persisten di billing_config.json.
LIMIT_HARIAN_DEFAULT = 5
JEDA_GENERATE_DEFAULT = 120
MAX_ANTREAN_DEFAULT = 20
MIN_TOPUP_DEFAULT = 10000
ESTIMASI_MENIT_DEFAULT = 3


def _cfg_int(key, default):
    try:
        return int(_config().get(key, default))
    except Exception:
        return default


def _cfg_set_int(key, val):
    cfg = _config()
    cfg[key] = val
    _save_config(cfg)
    return val


def limit_harian():
    return _cfg_int("limit_harian", LIMIT_HARIAN_DEFAULT)


def set_limit_harian(angka):
    angka = int(angka)
    if angka < 0:
        raise ValueError("limit minimal 0")
    return _cfg_set_int("limit_harian", angka)


def jeda_generate():
    return _cfg_int("jeda_generate", JEDA_GENERATE_DEFAULT)


def set_jeda_generate(detik):
    detik = int(detik)
    if detik < 0:
        raise ValueError("jeda minimal 0")
    return _cfg_set_int("jeda_generate", detik)


def max_antrean():
    return _cfg_int("max_antrean", MAX_ANTREAN_DEFAULT)


def set_max_antrean(angka):
    angka = int(angka)
    if angka < 1:
        raise ValueError("max antri minimal 1")
    return _cfg_set_int("max_antrean", angka)


def min_topup():
    return _cfg_int("min_topup", MIN_TOPUP_DEFAULT)


def set_min_topup(rupiah):
    rupiah = int(rupiah)
    if rupiah < 0:
        raise ValueError("nominal negatif")
    return _cfg_set_int("min_topup", rupiah)


def estimasi_menit():
    return _cfg_int("estimasi_menit", ESTIMASI_MENIT_DEFAULT)


def set_estimasi_menit(menit):
    menit = int(menit)
    if menit < 1:
        raise ValueError("estimasi minimal 1")
    return _cfg_set_int("estimasi_menit", menit)


def gen_harian_terpakai(uid):
    d = _user(uid)
    info = d.get("gen_harian") or {}
    if info.get("tgl") != _tgl_hari_ini():
        return 0
    try:
        return int(info.get("n", 0))
    except Exception:
        return 0


def cek_limit_harian(uid):
    # Return (boleh, pesan). 0 = tanpa batas.
    batas = limit_harian()
    if batas <= 0:
        return True, "ok"
    if gen_harian_terpakai(uid) >= batas:
        return False, "Batas harian tercapai (%d/hari). Coba lagi besok." % batas
    return True, "ok"


def cek_jeda(uid):
    # Return (boleh, pesan). Cek jeda minimum antar generate per user.
    jeda = jeda_generate()
    if jeda <= 0:
        return True, "ok"
    try:
        terakhir = float(_user(uid).get("terakhir_generate", 0) or 0)
    except Exception:
        terakhir = 0
    sisa = jeda - (time.time() - terakhir)
    if sisa > 0:
        return False, "Tunggu %s sebelum generate lagi." % fmt_sisa(int(sisa) + 1)
    return True, "ok"


def catat_generate(uid):
    # Tambah counter harian + catat waktu generate terakhir. Reset tiap tengah malam.
    d = _user(uid)
    info = d.get("gen_harian") or {}
    if info.get("tgl") != _tgl_hari_ini():
        info = {"tgl": _tgl_hari_ini(), "n": 0}
    try:
        n = int(info.get("n", 0))
    except Exception:
        n = 0
    info["n"] = n + 1
    d["gen_harian"] = info
    d["terakhir_generate"] = time.time()
    _save()
    return info["n"]


def get_google_api_key():
    return str(_config().get("GOOGLE_API_KEY") or "").strip()


def get_google_cx():
    return str(_config().get("GOOGLE_CX") or "").strip()


def set_google_api_key(key):
    cfg = _config()
    cfg["GOOGLE_API_KEY"] = (key or "").strip()
    _save_config(cfg)


def set_google_cx(cx):
    cfg = _config()
    cfg["GOOGLE_CX"] = (cx or "").strip()
    _save_config(cfg)



def harga_video_segmen(uid, tier, segmen):
    """Harga video Veo 3.1 Fast: harga tier x jumlah segmen 8 detik.

    8 detik = 1 segmen, 16 detik = 2 segmen, 24 detik = 3 segmen.
    Fast = 20 kredit vs Omni ~10 (2x biaya), tercermin via perkalian segmen.
    """
    return max(0, int(harga_tier_untuk(uid, tier)) * max(1, int(segmen)))
