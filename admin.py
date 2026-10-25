"""Admin control, ban, user tracking, anti-spam untuk Sutradara Bot."""
import json
import os
import time
from pathlib import Path

ADMIN_IDS = {u.strip() for u in
             os.environ.get("SUTRADARA_ADMINS", "7139434944").split(",")
             if u.strip()}
DATA_DIR = Path(os.environ.get("SUTRADARA_WORKDIR",
                               "/home/ihsan/sutradara-bot/work")).parent
USERS_FILE = DATA_DIR / "users.json"

FOOTER = "\n\nMade with \u2764\ufe0f by Sir Ihsan"


def ft(t: str | None) -> str:
    """Tempel footer ke teks pesan."""
    return (t or "") + FOOTER


def _load() -> dict:
    try:
        d = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


users_db: dict = _load()


def _save() -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(users_db, ensure_ascii=False), encoding="utf-8")
    tmp.replace(USERS_FILE)


def is_admin(uid) -> bool:
    return str(uid) in ADMIN_IDS


def catat_user(tu) -> dict:
    uid = str(tu.id)
    d = users_db.get(uid)
    if not isinstance(d, dict):
        d = {}
    now = time.time()
    d.update({
        "username": tu.username or "",
        "nama": f"{tu.first_name or ''} {tu.last_name or ''}".strip(),
        "link": f"https://t.me/{tu.username}" if tu.username else "",
        "last_seen": now,
    })
    d.setdefault("first_seen", now)
    d.setdefault("banned_until", 0)
    d.setdefault("ban_reason", "")
    d.setdefault("productions_total", 0)
    d.setdefault("prod_hari_ini", 0)
    d.setdefault("prod_tgl", "")
    d.setdefault("prod_terakhir", 0)
    users_db[uid] = d
    _save()
    return d


def cek_ban(uid):
    """Return dict user kalau sedang di-ban, else None.
    Ban kedaluwarsa dilepas otomatis."""
    d = users_db.get(str(uid))
    if not isinstance(d, dict):
        return None
    bu = d.get("banned_until", 0)
    if bu == -1:
        return d
    if bu and bu > time.time():
        return d
    if bu:
        d["banned_until"] = 0
        d["ban_reason"] = ""
        _save()
    return None


def ban_user(uid, durasi_detik=None, reason=""):
    """durasi_detik=None \u2192 ban permanen."""
    d = users_db.get(str(uid))
    if not isinstance(d, dict):
        d = {"username": "", "nama": "", "link": ""}
        users_db[str(uid)] = d
    d["banned_until"] = -1 if durasi_detik is None else time.time() + durasi_detik
    d["ban_reason"] = reason
    _save()


def unban_user(uid) -> None:
    d = users_db.get(str(uid))
    if isinstance(d, dict):
        d["banned_until"] = 0
        d["ban_reason"] = ""
        _save()


# Anti-spam: lindungi flow2api dari produksi beruntun
PROD_LIMIT_HARIAN = 5
PROD_JEDA_DETIK = 120


def boleh_produksi(uid):
    """(boleh, pesan). Admin bebas batas."""
    if is_admin(uid):
        return True, ""
    d = users_db.get(str(uid))
    if not isinstance(d, dict):
        return True, ""
    today = time.strftime("%Y-%m-%d")
    if d.get("prod_tgl") != today:
        d["prod_tgl"] = today
        d["prod_hari_ini"] = 0
        _save()
    if d.get("prod_hari_ini", 0) >= PROD_LIMIT_HARIAN:
        return False, (f"\u23f3 Jatah produksi harian kamu habis "
                       f"({PROD_LIMIT_HARIAN}x/hari). Besok lagi ya \U0001F64F")
    sisa = PROD_JEDA_DETIK - (time.time() - d.get("prod_terakhir", 0))
    if sisa > 0:
        return False, (f"\u23f3 Kasih jeda {int(sisa)} detik dulu "
                       "sebelum produksi berikutnya ya.")
    return True, ""

def catat_produksi(uid) -> None:
    d = users_db.get(str(uid))
    if not isinstance(d, dict):
        return
    today = time.strftime("%Y-%m-%d")
    if d.get("prod_tgl") != today:
        d["prod_tgl"] = today
        d["prod_hari_ini"] = 0
    d["prod_hari_ini"] = d.get("prod_hari_ini", 0) + 1
    d["productions_total"] = d.get("productions_total", 0) + 1
    d["prod_terakhir"] = time.time()
    _save()


def get_meta(k, default=None):
    m = users_db.get("_meta")
    if not isinstance(m, dict):
        return default
    return m.get(k, default)


def set_meta(k, v) -> None:
    m = users_db.get("_meta")
    if not isinstance(m, dict):
        m = {}
        users_db["_meta"] = m
    m[k] = v
    _save()


def stats() -> dict:
    today = time.strftime("%Y-%m-%d")
    users = [d for uid, d in users_db.items()
             if not uid.startswith("_") and isinstance(d, dict)]
    banned = sum(1 for d in users
                 if d.get("banned_until") == -1
                 or d.get("banned_until", 0) > time.time())
    prod_hari = sum(d.get("prod_hari_ini", 0) for d in users
                    if d.get("prod_tgl") == today)
    prod_total = sum(d.get("productions_total", 0) for d in users)
    return {"total_user": len(users), "banned": banned,
            "prod_hari_ini": prod_hari, "prod_total": prod_total}


def daftar_user(limit: int = 20) -> list:
    users = [(uid, d) for uid, d in users_db.items()
             if not uid.startswith("_") and isinstance(d, dict)]
    users.sort(key=lambda x: x[1].get("last_seen", 0), reverse=True)
    return users[:limit]
