"""Sistem antrean generate video (FASE 2).

Urutan: weighted fair queue 2:1 (2 prioritas, 1 reguler, berulang), FIFO dalam tier yang sama.
Disimpan di queue.json sebagai list of {uid, tier, job_data, created_at}.
"""
import json
import time
from pathlib import Path

from admin import DATA_DIR

QUEUE_FILE = DATA_DIR / "queue.json"
ESTIMASI_MENIT_PER_VIDEO = 3

_sedang_diproses = None
_fair_counter = 0  # FASE 4: counter weighted fair queue 2:1


def _load():
    try:
        d = json.loads(Path(QUEUE_FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save(q):
    path = Path(QUEUE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(q, ensure_ascii=True, indent=1), encoding="utf-8")
    tmp.replace(path)


def tambah_antrean(uid, tier, job_data):
    tier = "prioritas" if str(tier) == "prioritas" else "reguler"
    k = str(uid)
    if _sedang_diproses and str(_sedang_diproses.get("uid")) == k:
        return 1
    q = _load()
    for j in q:
        if str(j.get("uid")) == str(uid):
            return posisi_antrean(uid)
    job = {"uid": str(uid), "tier": tier, "job_data": job_data,
           "created_at": time.time()}
    if tier == "prioritas":
        i = 0
        while i < len(q) and q[i].get("tier") == "prioritas":
            i += 1
        q.insert(i, job)
    else:
        q.append(job)
    _save(q)
    return posisi_antrean(uid)


def ambil_berikutnya():
    # FASE 4: weighted fair queue 2:1 (2 prioritas, 1 reguler, berulang).
    # Jika salah satu tier kosong, ambil dari tier yang ada (tidak idle).
    global _sedang_diproses, _fair_counter
    q = _load()
    if not q:
        _sedang_diproses = None
        return None
    ada_p = any(j.get("tier") == "prioritas" for j in q)
    ada_r = any(j.get("tier") != "prioritas" for j in q)
    if ada_p and ada_r:
        tier_pilih = "prioritas" if (_fair_counter % 3) < 2 else "reguler"
    elif ada_p:
        tier_pilih = "prioritas"
    else:
        tier_pilih = "reguler"
    idx = None
    for i, j in enumerate(q):
        if (j.get("tier") == "prioritas") == (tier_pilih == "prioritas"):
            idx = i
            break
    if idx is None:
        idx = 0  # fallback ke item pertama jika tier yang diinginkan tidak ada
    job = q.pop(idx)
    _fair_counter += 1
    _save(q)
    _sedang_diproses = job
    return job


def sedang_diproses():
    return _sedang_diproses


def update_job_data(uid, extra_data: dict):
    """Update job_data untuk user di antrean."""
    q = _load()
    changed = False
    for j in q:
        if str(j.get("uid")) == str(uid):
            jd = j.get("job_data") or {}
            jd.update(extra_data)
            j["job_data"] = jd
            changed = True
            break
    if changed:
        _save(q)


def selesai_diproses():
    global _sedang_diproses
    _sedang_diproses = None


def posisi_antrean(uid):
    k = str(uid)
    if _sedang_diproses and str(_sedang_diproses.get("uid")) == k:
        return 1
    base = 2 if _sedang_diproses else 1
    for i, j in enumerate(_load()):
        if str(j.get("uid")) == k:
            return base + i
    return 0


def batal_antrean(uid):
    k = str(uid)
    q = _load()
    baru = [j for j in q if str(j.get("uid")) != k]
    if len(baru) == len(q):
        return False
    _save(baru)
    return True


def estimasi_waktu():
    n = len(_load()) + (1 if _sedang_diproses else 0)
    try:
        import billing
        menit = int(billing.estimasi_menit())
    except Exception:
        menit = ESTIMASI_MENIT_PER_VIDEO
    return n * menit


def panjang_antrean():
    return len(_load())
