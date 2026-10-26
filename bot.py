"""Sutradara Bot v3 \u2014 storyboard sheet, UX edit-pesan."""
import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

from research import research_products, riset_caption
from webcatalog import (
    cari_kandidat_katalog, bedah_link, unduh_gambar, validasi_kandidat,
    extract_first_url,
)
from admin import (
    ft, FOOTER, ADMIN_IDS, users_db, is_admin, catat_user, cek_ban,
    ban_user, unban_user, boleh_produksi, catat_produksi,
    get_meta, set_meta, stats, daftar_user,
)
from storyboard import (
    generate_storyboard, build_video_prompt, ENHANCE_PROMPT, catalog_prompt,
    ANOMALY_LOCK, CONSISTENCY_LOCK, _call_omni, analyze_catalog_variants,
    build_frame_prompt,
)
import billing
import antrean
from flowclient import FlowClient
from sheet import compose_sheet

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("sutradara-bot")

BOT_TOKEN = os.environ.get("SUTRADARA_BOT_TOKEN", "")
FLOW_BASE = os.environ.get("FLOW2API_BASE", "http://127.0.0.1:8090")
FLOW_KEY = os.environ.get("FLOW2API_KEY", "sirihsan")
WORKDIR = Path(os.environ.get("SUTRADARA_WORKDIR", "/home/ihsan/sutradara-bot/work"))
ALLOWED_USERS = {u.strip() for u in os.environ.get("SUTRADARA_ALLOWED", "").split(",") if u.strip()}

(MENU, RISET_KAT, PILIH_PRODUK, MANUAL_NAMA, MANUAL_DESK,
 OPSI_STYLE, OPSI_AUDIO, OPSI_FRAME, OPSI_DURASI, FOTO) = range(10)
(APPEAL_TULIS, BC_TULIS, BC_KONFIRM) = range(10, 13)
(TOPUP_NOMINAL, TOPUP_BAYAR, TOPUP_CUSTOM) = range(13, 16)
REDEEM_KODE = 16
B = InlineKeyboardButton  # alias tombol, dipakai kb_mode dkk

job_lock = asyncio.Lock()
sessions = {}
_user_async_locks = {}
_user_click_timestamps = {}


def get_user_lock(uid: int) -> asyncio.Lock:
    if uid not in _user_async_locks:
        _user_async_locks[uid] = asyncio.Lock()
    return _user_async_locks[uid]


def is_spam_click(uid: int, cooldown_sec: float = 2.0) -> bool:
    """Cek apakah tombol ditekan berulang kali dalam jeda waktu singkat (anti-double-click)."""
    now = time.time()
    last = _user_click_timestamps.get(uid, 0.0)
    if now - last < cooldown_sec:
        return True
    _user_click_timestamps[uid] = now
    return False


def proses_user_aktif(uid: int) -> tuple[str, str] | None:
    """Cek apakah user masih memiliki proses aktif yang sedang berjalan.
    Mengembalikan (tipe_proses, pesan_keterangan) atau None jika tidak ada proses aktif.
    """
    uid_str = str(uid)
    s = sessions.get(uid) or {}

    # 1. Cek apakah video user sedang aktif digenerate oleh worker / task lokal
    sedang = antrean.sedang_diproses()
    if sedang and str(sedang.get("uid")) == uid_str:
        return (
            "video_generating",
            "⚠️ *Masih ada video kamu yang sedang diproses!* Mohon tunggu sampai selesai dulu ya, jangan membuat atau mengklik proses baru."
        )

    vt = s.get("video_task")
    if vt and not vt.done() and not vt.cancelled():
        return (
            "video_generating",
            "⚠️ *Masih ada video kamu yang sedang diproses!* Mohon tunggu sampai selesai dulu ya, jangan membuat atau mengklik proses baru."
        )

    if s.get("is_generating_video"):
        return (
            "video_generating",
            "⚠️ *Masih ada video kamu yang sedang diproses!* Mohon tunggu sampai selesai dulu ya, jangan membuat atau mengklik proses baru."
        )

    # 2. Cek apakah user sedang menunggu di antrean
    pos = antrean.posisi_antrean(uid)
    if pos > 0:
        menit = antrean.estimasi_waktu()
        return (
            "antrean_waiting",
            f"⏳ *Kamu masih memiliki antrean video yang belum selesai (Posisi #{pos}, estimasi ~{menit} menit)!* Mohon tunggu giliranmu ya, jangan membuat antrean baru."
        )

    # 3. Cek apakah produksi storyboard / katalog awal masih berjalan
    pt = s.get("produksi_task")
    if pt and not pt.done() and not pt.cancelled():
        return (
            "storyboard_producing",
            "⚠️ *Masih ada proses pembuatan storyboard / katalog yang sedang berjalan!* Mohon tunggu sebentar sampai prosesnya selesai ya."
        )

    if s.get("is_producing_storyboard"):
        return (
            "storyboard_producing",
            "⚠️ *Masih ada proses pembuatan storyboard / katalog yang sedang berjalan!* Mohon tunggu sebentar sampai prosesnya selesai ya."
        )

    return None

STYLES = {"ugc": "\U0001F4F1 UGC Natural", "sinematik": "\U0001F3AC Sinematik",
          "studio": "\u2728 Studio Iklan Clean", "aesthetic": "\U0001F338 Aesthetic Pastel"}
AUDIOS = {"vo": "\U0001F399\ufe0f Voice Over Indonesia",
          "vo_en": "\U0001F399\ufe0f Voice Over English",
          "asmr": "\U0001F3A7 ASMR (Suara Produk, Tanpa VO)",
          "musik": "\U0001F3B5 Musik Aja (Tanpa VO)",
          "asmr_vo": "\U0001F3A7\U0001F399\ufe0f ASMR + VO ElevenLabs (Edit Manual)"}
FRAMES = {"fullbody": "\U0001F9CD Full Body", "setengah": "\U0001F464 Setengah Badan",
          "tangan": "\u270b Tangan Aja", "produk": "\U0001F4E6 Produk Aja",
          "flatlay": "\U0001F532 Flat Lay"}


def new_session(uid: int) -> dict:
    old = sessions.get(uid) or {}
    s = {"produk": "", "deskripsi": "", "kandidat": [],
         "style": "ugc", "audio": "vo", "framing": "tangan",
         "durasi": 8, "board": None, "frames": [],
         "status_id": None, "workdir": None, "katalog": None,
         "video_task": old.get("video_task"),
         "is_generating_video": old.get("is_generating_video", False),
         "produksi_task": old.get("produksi_task"),
         "is_producing_storyboard": old.get("is_producing_storyboard", False)}
    sessions[uid] = s
    return s


def get_session(uid: int) -> dict:
    return sessions.get(uid) or new_session(uid)


def allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    u = update.effective_user
    return str(u.id) in ALLOWED_USERS or (u.username or "") in ALLOWED_USERS


def kb_grid(items: dict, prefix: str, cols: int = 2, back: bool = False) -> InlineKeyboardMarkup:
    btns = [InlineKeyboardButton(label, callback_data=f"{prefix}:{k}")
            for k, label in items.items()]
    rows = [btns[i:i + cols] for i in range(0, len(btns), cols)]
    if back:
        rows.append([InlineKeyboardButton("\u2b05\ufe0f Kembali", callback_data="nav:back")])
    return InlineKeyboardMarkup(rows)


def kb_back_only() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("\u2b05\ufe0f Kembali", callback_data="nav:back")]])


# ===== FITUR A: tombol Otomatis (AI pilihkan) di langkah style/audio/framing =====
_AUTO_SYS = ("Kamu asisten yang milihin opsi video iklan produk. "
             "Jawab SELALU dengan JSON valid saja, tanpa teks lain.")
_AUTO_DEFAULT = {"style": "sinematik", "audio": "vo", "framing": "fullbody"}
_AUTO_STATE = {"style": OPSI_STYLE, "audio": OPSI_AUDIO, "frame": OPSI_FRAME}


def _kb_auto(kb: InlineKeyboardMarkup, cb: str) -> InlineKeyboardMarkup:
    rows = [list(r) for r in kb.inline_keyboard]
    auto = [InlineKeyboardButton("\U0001F916 Otomatis (AI pilihkan)",
                                 callback_data=cb)]
    rows = rows[:-1] + [auto] + rows[-1:] if rows else [auto]
    return InlineKeyboardMarkup(rows)


def _ui_style(s: dict):
    return (ft("Sekarang pilih style videonya:"),
            _kb_auto(kb_grid(STYLES, "style", back=True), "auto:style"))


def _ui_audio(s: dict):
    return (ft("Style: " + STYLES[s["style"]] + "\n\nAudionya mau yang gimana?"),
            _kb_auto(kb_grid(AUDIOS, "audio", cols=1, back=True), "auto:audio"))


def _ui_frame(s: dict):
    return (ft("Audio: " + AUDIOS[s["audio"]] + "\n\nFraming-nya (yang keliatan di video) mau apa?"),
            _kb_auto(kb_grid(FRAMES, "frame", back=True), "auto:frame"))


def _ui_durasi(s: dict):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("8 dtk", callback_data="dur:8"),
         InlineKeyboardButton("16 dtk", callback_data="dur:16"),
         InlineKeyboardButton("24 dtk", callback_data="dur:24")],
        [InlineKeyboardButton("\u2b05\ufe0f Kembali", callback_data="nav:back")]])
    return (ft("Framing: " + FRAMES[s["framing"]] + "\n\nTerakhir, durasinya berapa detik?"), kb)


def _parse_auto_json(raw: str):
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    style = str(d.get("style", "")).strip().lower()
    audio = str(d.get("audio", "")).strip().lower()
    framing = str(d.get("framing", "")).strip().lower()
    alasan = str(d.get("alasan_singkat", "")).strip()
    if framing == "full":
        framing = "fullbody"
    if style not in STYLES or audio not in AUDIOS or framing not in FRAMES:
        return None
    return {"style": style, "audio": audio, "framing": framing,
            "alasan": alasan}


async def _minta_auto(produk: str, deskripsi: str):
    user = (f"Produk: {produk} - {deskripsi}. Pilihkan yang paling cocok: "
            "style [ugc/sinematik/studio/aesthetic], audio [vo/vo_en/asmr/musik/asmr_vo], "
            "framing [full/setengah/tangan/produk/flatlay]. "
            "Jawab JSON: {style, audio, framing, alasan_singkat}.")
    for _ in range(3):
        try:
            raw = await asyncio.to_thread(_call_omni, _AUTO_SYS, user)
        except Exception:
            continue
        hasil = _parse_auto_json(raw)
        if hasil:
            return hasil
    return None


# ===== FITUR B: layar konfirmasi sebelum generate / tier =====
def _teks_konfirmasi(s: dict, uid: int) -> str:
    if is_admin(uid) or billing.is_unlimited(uid):
        harga = "Gratis"
    else:
        harga = "Rp " + billing.rp(billing.harga_tier_untuk(uid, "reguler"))
    baris = [
        "\U0001F4CB Konfirmasi Video",
        "Produk: " + str(s.get("produk")),
        "Style: " + str(STYLES.get(s.get("style"), s.get("style"))),
        "Audio: " + str(AUDIOS.get(s.get("audio"), s.get("audio"))),
        "Framing: " + str(FRAMES.get(s.get("framing"), s.get("framing"))),
        "Durasi: " + str(s.get("durasi")) + " detik",
        "Estimasi harga: " + harga,
    ]
    return "\n".join(baris)


def _kb_konfirmasi() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u270f\ufe0f Edit", callback_data="gas:edit"),
         InlineKeyboardButton("\U0001F680 Gas Bikin Video", callback_data="gas:go")],
    ])


# Edit pesan dari callback: dukung caption (foto) maupun teks biasa.
async def _edit_pesan_q(update, context, s, teks, kb):
    q = update.callback_query
    try:
        await q.edit_message_caption(caption=ft(teks), reply_markup=kb)
        return
    except Exception:
        pass
    try:
        await q.edit_message_text(ft(teks), reply_markup=kb)
        return
    except Exception:
        pass
    await say(context, q.message.chat_id, s, teks, reply_markup=kb, fresh=True)


async def _tampilkan_konfirmasi(update, context, s):
    q = update.callback_query
    await _edit_pesan_q(update, context, s,
                        _teks_konfirmasi(s, q.from_user.id),
                        _kb_konfirmasi())


async def _tampilkan_menu_edit(update, context, s):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(chr(0x1F3A8)+" Style", callback_data="gas:edit:style"),
         InlineKeyboardButton(chr(0x1F3B5)+" Audio", callback_data="gas:edit:audio")],
        [InlineKeyboardButton(chr(0x1F5BC)+" Framing", callback_data="gas:edit:frame"),
         InlineKeyboardButton(chr(0x23F1)+" Durasi", callback_data="gas:edit:durasi")],
        [InlineKeyboardButton("\u2b05\ufe0f Kembali", callback_data="gas:kembali")],
    ])
    await _edit_pesan_q(update, context, s, "Mau edit bagian yang mana?", kb)


# Setelah user milih opsi: balik ke konfirmasi kalau lagi mode edit.
async def _setelah_pilih(update, context, s, teks, kb, lanjut):
    q = update.callback_query
    if s.pop("dari_konfirmasi", None):
        await _tampilkan_konfirmasi(update, context, s)
        return ConversationHandler.END
    await q.edit_message_text(teks, reply_markup=kb)
    return lanjut


async def auto_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = sessions.get(q.from_user.id)
    if not s:
        return ConversationHandler.END
    step = q.data.split(":")[1]
    s["auto_step"] = step
    s["status_id"] = q.message.message_id
    await q.edit_message_text(ft("\U0001F916 Lagi mikir, pilihin yang paling cocok buat produk lo ya..."))
    hasil = await _minta_auto(s.get("produk", ""), s.get("deskripsi", ""))
    if hasil:
        note = "\U0001F916 AI udah milihin buat lo:\n"
    else:
        hasil = dict(_AUTO_DEFAULT)
        note = "AI lagi sibuk, pake default ya:\n"
    s["auto_hasil"] = hasil
    alasan = " (" + hasil["alasan"] + ")" if hasil.get("alasan") else ""
    teks = (note
            + "Style: " + STYLES[hasil["style"]] + alasan + "\n"
            + "Audio: " + AUDIOS[hasil["audio"]] + "\n"
            + "Framing: " + FRAMES[hasil["framing"]] + "\n\n"
            + "Oke?")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2705 Pakai", callback_data="auto:ok"),
         InlineKeyboardButton("\U0001F504 Pilih Manual", callback_data="auto:manual")],
    ])
    await q.edit_message_text(ft(teks), reply_markup=kb)
    return _AUTO_STATE.get(step, OPSI_STYLE)


async def auto_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = sessions.get(q.from_user.id)
    hasil = (s or {}).pop("auto_hasil", None)
    if s:
        s.pop("auto_step", None)
    if not s or not hasil:
        return ConversationHandler.END
    s["style"], s["audio"], s["framing"] = hasil["style"], hasil["audio"], hasil["framing"]
    s["status_id"] = q.message.message_id
    teks, kb = _ui_durasi(s)
    return await _setelah_pilih(update, context, s, teks, kb, OPSI_DURASI)


async def auto_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = sessions.get(q.from_user.id)
    if not s:
        return ConversationHandler.END
    s.pop("auto_hasil", None)
    step = s.pop("auto_step", None) or "style"
    s["status_id"] = q.message.message_id
    ui = {"audio": (_ui_audio, OPSI_AUDIO), "frame": (_ui_frame, OPSI_FRAME)}
    fn, st = ui.get(step, (_ui_style, OPSI_STYLE))
    teks, kb = fn(s)
    await q.edit_message_text(teks, reply_markup=kb)
    return st


async def edit_mulai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = sessions.get(q.from_user.id)
    if not s:
        return ConversationHandler.END
    s["dari_konfirmasi"] = True
    s["status_id"] = q.message.message_id
    bagian = q.data.split(":")[2]
    ui = {"audio": (_ui_audio, OPSI_AUDIO),
          "frame": (_ui_frame, OPSI_FRAME),
          "durasi": (_ui_durasi, OPSI_DURASI)}
    fn, st = ui.get(bagian, (_ui_style, OPSI_STYLE))
    teks, kb = fn(s)
    await q.edit_message_text(teks, reply_markup=kb)
    return st


_AUTO_HANDLERS = [
    CallbackQueryHandler(auto_ask, pattern=r"^auto:(style|audio|frame)$"),
    CallbackQueryHandler(auto_ok, pattern=r"^auto:ok$"),
    CallbackQueryHandler(auto_manual, pattern=r"^auto:manual$"),
]



def kb_opsi_foto(s: dict) -> InlineKeyboardMarkup:
    rows = []
    if s.get("katalog"):
        rows.append([InlineKeyboardButton("\u2705 Pake Katalog dari Link",
                                          callback_data="foto:keep")])
    rows.append([InlineKeyboardButton("\u2728 Generate Katalog AI",
                                      callback_data="foto:ai")])
    rows.append([InlineKeyboardButton("\U0001F50D Cari Katalog Mirip di Web",
                                      callback_data="foto:web")])
    rows.append([InlineKeyboardButton("\u2b05\ufe0f Kembali", callback_data="nav:back")])
    return InlineKeyboardMarkup(rows)


def teks_opsi_foto(s: dict) -> str:
    t = "\U0001F4F8 "
    if s.get("katalog"):
        t += ("Katalognya udah ada (dari link yang lo kirim tadi \U0001F44C) \u2014 "
              "mau pake itu, kirim foto sendiri, atau pilih salah satu:")
    else:
        t += ("Kirim *foto produknya* (yang jelas produknya ya), "
              "atau pilih salah satu:")
    return t


DISCLAIMER_AI = (
    "\u26a0\ufe0f *Disclaimer dulu ya*\n\n"
    "Katalog AI ini gw *generate dari deskripsi produk* \u2014 jadi hasilnya "
    "*nggak 100% sama persis* sama barang aslinya. Bisa beda dikit di "
    "warna, detail, atau bentuknya.\n\n"
    "Buat bahan video affiliate sih aman, tapi jangan dipake buat katalog "
    "jualan beneran ya \U0001F64F\n\n"
    "Lanjut generate?")

def kb_menu(uid) -> InlineKeyboardMarkup:
    # FASE 4: menu utama dengan tombol.
    B = InlineKeyboardButton
    rows = [
        [B(chr(0x1F3AC) + " Generate Video", callback_data="menu:generate")],
        [B(chr(0x1F4B0) + " Saldo", callback_data="menu:saldo"), B(chr(0x1F4E5) + " Topup", callback_data="menu:topup")],
        [B(chr(0x1F3AB) + " Redeem", callback_data="menu:redeem"), B(chr(0x1F465) + " Referal", callback_data="menu:referal")],
    ]
    if is_admin(uid):
        rows += [
            [B(chr(0x1F4CA) + " Dasbor", callback_data="menu:dasbor"), B(chr(0x1F4B2) + " Harga", callback_data="menu:harga")],
            [B(chr(0x1F39F) + " Voucher", callback_data="menu:voucher"), B(chr(0x23F3) + " Topup Pending", callback_data="menu:topup_pending")],
            [B(chr(0x2699)+chr(0xFE0F) + " Settings", callback_data="menu:settings")],
        ]
    return InlineKeyboardMarkup(rows)


def kb_mode() -> InlineKeyboardMarkup:
    # FASE 4: pilihan mode generate.
    return InlineKeyboardMarkup([
        [B(chr(0x1F525) + " Riset Otomatis (Produk Viral)", callback_data="menu:riset")],
        [B(chr(0x270D)+chr(0xFE0F) + " Manual (Produk Sendiri)", callback_data="menu:manual")],
        [B(chr(0x2B05)+chr(0xFE0F) + " Kembali", callback_data="nav:back")],
    ])


MENU_TEXT = "\U0001F3AC\u2728 SUTRADARA BOT \u2728\U0001F3AC\n\nHalo, San! \U0001F44B\nGw studio video AI lo - dari riset produk, storyboard, sampe jadi video affiliate 8/16/24 detik, semua otomatis. Lo tinggal pilih-pilih aja.\n\nProduknya mau dari mana nih? \U0001F447"


async def say(context: ContextTypes.DEFAULT_TYPE, chat_id: int, s: dict,
              text: str, reply_markup=None, parse_mode: str | None = None,
              fresh: bool = False):
    """Edit pesan status yang sama. fresh=True: hapus status lama & buat pesan
    baru \u2014 dipakai setelah user kirim pesan, biar status selalu muncul DI BAWAH
    pesan terakhir user (user nggak ngira pesannya belum diproses).
    Hasil (foto/video) TIDAK lewat sini \u2014 selalu pesan baru."""
    text = ft(text)
    mid = s.get("status_id")
    if fresh and mid:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass
        s["status_id"] = None
        mid = None
    if mid:
        try:
            await context.bot.edit_message_text(
                text, chat_id=chat_id, message_id=mid,
                reply_markup=reply_markup, parse_mode=parse_mode)
            return
        except Exception:
            pass
    m = await context.bot.send_message(chat_id, text,
                                       reply_markup=reply_markup,
                                       parse_mode=parse_mode)
    s["status_id"] = m.message_id


def jam_wib():
    # Jam WIB sekarang, format HH:MM. Untuk timestamp pesan progress.
    return time.strftime("%H:%M", time.localtime())


def progress_ts(text):
    # Bubuhkan timestamp (HH:MM) pada pesan progress agar user bisa
    # membedakan pesan baru vs pesan basi yang tertinggal.
    return f"{text} ({jam_wib()})"


async def _tandai_progress_gagal(bot_or_app, chat_id, s, ket):
    # Jaring pengaman: bila generate gagal di luar except utama (mis.
    # CancelledError saat service restart), pastikan pesan "Lagi bikin..."
    # tidak menggantung begitu saja.
    if not isinstance(s, dict):
        return
    mid = s.get("status_id")
    if not mid:
        return
    bot = getattr(bot_or_app, "bot", bot_or_app)
    try:
        await bot.edit_message_text(
            ft("Proses " + str(ket) + " terhenti; pesan progress ini sudah tidak berlaku."),
            chat_id=chat_id, message_id=mid)
    except Exception:
        pass


async def bersihkan_progress_basi(bot_or_app, chat_id, sess, lewati_id=None):
    # Hapus/edit pesan progress basi dari sesi sebelumnya bila masih ada.
    # Dipanggil saat user mulai generate baru. Pesan yang sedang dibuka
    # (lewati_id) tidak disentuh.
    if not isinstance(sess, dict):
        return
    mid = sess.get("status_id")
    if not mid or (lewati_id is not None and mid == lewati_id):
        return
    bot = getattr(bot_or_app, "bot", bot_or_app)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=mid)
    except Exception:
        try:
            await bot.edit_message_text(
                ft("(pesan progress lama - sudah tidak berlaku)"),
                chat_id=chat_id, message_id=mid)
        except Exception:
            pass
    sess["status_id"] = None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text(ft("Maaf ya, bot ini privat."))
        return ConversationHandler.END
    catat_user(update.effective_user)
    if get_meta("maintenance", False) and not is_admin(update.effective_user.id):
        await update.message.reply_text(
            ft("\U0001F527 Bot lagi maintenance bentar ya. Coba lagi nanti \U0001F64F"))
        return ConversationHandler.END
    if await tolak_kalau_ban(update, context):
        return ConversationHandler.END
    # --- FASE 2: REFERAL (parse /start ref_<id>) ---
    if context.args:
        _arg0 = str(context.args[0] or "")
        if _arg0.startswith("ref_"):
            try:
                _uid_ref = int(_arg0[4:])
            except ValueError:
                _uid_ref = 0
            _uid_saya = update.effective_user.id
            if _uid_ref and _uid_ref != _uid_saya:
                _ok, _pesan = billing.catat_referal(_uid_saya, _uid_ref)
                if _ok:
                    await update.message.reply_text(
                        ft("Bonus referal! Saldo kamu +%s."
                           % billing.rp(billing.bonus_baru_efektif())))
                    try:
                        await context.bot.send_message(
                            chat_id=_uid_ref,
                            text=ft("Temanmu join, +%s."
                                    % billing.rp(billing.bonus_pengundang_efektif())))
                    except Exception:
                        pass
    # Bersihkan pesan progress basi
    sesi_lama = sessions.get(update.effective_user.id)
    if sesi_lama is not None:
        await bersihkan_progress_basi(context, update.effective_chat.id, sesi_lama)
    s = new_session(update.effective_user.id)
    m = await update.message.reply_text(ft(MENU_TEXT), reply_markup=kb_menu(update.effective_user.id))
    s["status_id"] = m.message_id
    return MENU


async def _menu_aksi(update, context):
    # FASE 4: jalankan command dari tombol menu.
    q = update.callback_query
    aksi = {
        "menu:saldo": saldo_cmd,
        "menu:referal": referal_cmd,
        "menu:dasbor": dasbor_cmd,
        "menu:voucher": voucher_list_cmd,
        "menu:topup_pending": topup_list_cmd,
    }.get(q.data)
    if aksi is None:
        return MENU
    # no shim: PTB v20+ forbids setting update.message; _balas_menu edits via callback_query
    await aksi(update, context)
    return MENU


async def _ck(q,uid):
    if is_admin(uid) or billing.is_unlimited(uid):
        return False
    if billing.get_saldo(uid)>=billing.harga_tier_untuk(uid,"reguler"):
        return False
    kb=InlineKeyboardMarkup([[InlineKeyboardButton(chr(0x1F4B3)+" T",callback_data="menu:topup")],[InlineKeyboardButton(chr(0x1F3E0)+" M",callback_data="menu:utama")]])
    await q.edit_message_text(chr(0x1F4B0)+" Saldo kurang.",reply_markup=kb)
    return True

async def menu_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if await tolak_kalau_ban(update, context):
        return
    s = sessions.get(q.from_user.id)
    if s is None:
        return ConversationHandler.END
    if q.data == "menu:generate":
        aktif = proses_user_aktif(q.from_user.id)
        if aktif:
            tipe, pesan = aktif
            popup_pesan = "⚠️ Masih ada proses yang belum selesai, tunggu dulu!"
            if tipe in ("video_generating", "antrean_waiting"):
                pos = antrean.posisi_antrean(q.from_user.id)
                pos_txt = f" (Posisi antrean #{pos})" if pos > 0 else ""
                popup_pesan = f"⚠️ Masih ada video yang sedang diproses{pos_txt}! Tunggu sampai selesai ya."
            elif tipe == "storyboard_producing":
                popup_pesan = "⚠️ Masih ada proses storyboard yang sedang berjalan! Tunggu sebentar ya."
            try:
                await q.answer(popup_pesan, show_alert=True)
            except Exception:
                pass
            await say(context, q.message.chat_id, s, pesan, fresh=True)
            return MENU
        await bersihkan_progress_basi(context, q.message.chat_id, s, lewati_id=q.message.message_id)
    s["status_id"] = q.message.message_id
    # FASE 4: tombol menu utama
    if q.data == "menu:utama":
        return await menu_utama_cb(update, context)
    if q.data == "menu:back":
        return await menu_back_cb(update, context)
    if q.data == "menu:generate":
        await q.edit_message_text(
            ft("Mau bikin video pakai mode apa?"),
            reply_markup=kb_mode())
        return MENU
    if q.data in ("menu:saldo", "menu:referal",
                  "menu:dasbor", "menu:voucher", "menu:topup_pending"):
        return await _menu_aksi(update, context)
    if q.data == "menu:redeem":
        await _edit_q(q, "Kirim *kode voucher* kamu:",
                      parse_mode="Markdown", reply_markup=kb_kembali_utama())
        return REDEEM_KODE
    if q.data == "menu:topup":
        return await _topup_tampil_nominal_menu(update, context)
    if q.data == "menu:harga":
        if not is_admin(q.from_user.id):
            await q.answer("Khusus admin.", show_alert=True)
            return MENU
        await _edit_q(q, chr(10).join([
            "Harga saat ini:",
            "Reguler: Rp %s" % billing.rp(billing.harga_reguler()),
            "Prioritas: Rp %s" % billing.rp(billing.harga_prioritas()),
            "Batas harian unlimited: %d/hari" % billing.batas_unlimited_harian()]),
            reply_markup=kb_kembali_utama())
        return MENU
    if q.data == "menu:settings":
        if not is_admin(q.from_user.id):
            await q.answer("Khusus admin.", show_alert=True)
            return MENU
        sessions.setdefault(q.from_user.id, {})["set_pending"] = None
        await _set_tampilkan(q)
        return MENU
    if q.data == "menu:riset":
        if await _ck(q,q.from_user.id):
            return MENU
        await q.edit_message_text(
            ft("\U0001F525 *Riset Otomatis*\n\nMau riset kategori apa nih? Ketik aja kategorinya "
            "(misal: *dapur*, *skincare*, *gadget*) atau ketik *bebas* biar gw yang mikir:"),
            parse_mode="Markdown", reply_markup=kb_back_only())
        return RISET_KAT
    if q.data=="menu:manual":
        if await _ck(q,q.from_user.id):
            return MENU
        await q.edit_message_text(
            ft("\u270d\ufe0f *Mode Manual*\n\nSiap! Kirim *nama produk* yang mau lo buat videonya:"),
            parse_mode="Markdown", reply_markup=kb_back_only())
        return MANUAL_NAMA

async def riset_kategori(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _mode_admin_aktif(update) or await tolak_kalau_ban(update, context):
        return None
    s = get_session(update.effective_user.id)
    chat_id = update.effective_chat.id
    kat = update.message.text.strip()
    if kat.lower() == "bebas":
        kat = ""
    await say(context, chat_id, s, "\U0001F50D Bentar ya, lagi gw risetin produk paling laris & berpotensi rame...",
              fresh=True)
    stop_riset = asyncio.Event()
    pmsg_riset = await context.bot.send_message(
        chat_id, "Lagi gw risetin produknya... 0%" + chr(10) + chr(9617) * 10)
    task_riset = asyncio.create_task(
        _progress_umum(pmsg_riset, "Lagi gw risetin produknya", 45, stop_riset))
    try:
        kandidat = await research_products(kat)
    except Exception as e:
        await say(context, chat_id, s, f"\U0001F605 Risetnya gagal nih: {e}\nCoba /start lagi ya.")
        return ConversationHandler.END
    finally:
        stop_riset.set()
        try:
            await pmsg_riset.delete()
        except Exception:
            pass
    s["kandidat"] = kandidat
    btns = [[InlineKeyboardButton(f"{i+1}. {k['nama'][:40]}", callback_data=f"prod:{i}")]
            for i, k in enumerate(kandidat)]
    btns.append([InlineKeyboardButton("\u2b05\ufe0f Kembali", callback_data="nav:back")])
    teks = "\U0001F525 *Hasil riset \u2014 produk yang berpotensi rame:*\n"
    for i, k in enumerate(kandidat):
        teks += (f"\n*{i+1}. {k['nama']}* ({k.get('harga_kisaran','')})\n"
                 f"_{k.get('alasan','')}_")
    teks += "\n\n\U0001F447 Tap produk yang lo mau ya:"
    await say(context, chat_id, s, teks, reply_markup=InlineKeyboardMarkup(btns),
              parse_mode="Markdown")
    return PILIH_PRODUK

async def produk_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = sessions.get(q.from_user.id)
    if s is None:
        return ConversationHandler.END
    s["status_id"] = q.message.message_id
    k = s["kandidat"][int(q.data.split(":")[1])]
    s["produk"] = k["nama"]
    s["deskripsi"] = k.get("deskripsi", "")
    await q.edit_message_text(
        ft(f"Oke, produknya: {k['nama']}\n\nSekarang pilih style videonya:"),
        reply_markup=_kb_auto(kb_grid(STYLES, "style", back=True), "auto:style"))
    return OPSI_STYLE


async def manual_nama(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _mode_admin_aktif(update) or await tolak_kalau_ban(update, context):
        return None
    s = get_session(update.effective_user.id)
    chat_id = update.effective_chat.id
    teks = (update.message.text or "").strip()
    found_url = extract_first_url(teks)
    if found_url:
        s["produk"] = ""
        return await manual_link(update, context, s, chat_id, found_url)
    s["produk"] = teks
    await say(context, chat_id, s,
              f"\u2705 Produk: *{s['produk']}*\n\nCeritain produknya dikit ya \u2014 "
              "*deskripsi produknya* "
              "(bentuk, warna, bahan, fungsi, keunggulan \u2014 makin detail makin akurat hasilnya).\n\n"
              "Atau *paste link produknya* langsung (Shopee / TikTok Shop / Tokopedia / link apa aja) \u2014 "
              "biar gw yang bedah + ambil gambarnya sekalian \U0001F517",
              parse_mode="Markdown", reply_markup=kb_back_only(), fresh=True)
    return MANUAL_DESK

async def manual_deskripsi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _mode_admin_aktif(update) or await tolak_kalau_ban(update, context):
        return None
    s = get_session(update.effective_user.id)
    chat_id = update.effective_chat.id
    teks = (update.message.text or "").strip()
    found_url = extract_first_url(teks)
    if found_url:
        return await manual_link(update, context, s, chat_id, found_url)
    s["deskripsi"] = teks
    await say(context, chat_id, s, "\U0001F3A8 Pilih *style video*:",
              reply_markup=kb_grid(STYLES, "style", back=True), parse_mode="Markdown",
              fresh=True)
    return OPSI_STYLE

async def manual_link(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      s: dict, chat_id: int, url: str):
    """Bedah link produk: ambil info + gambar jadi katalog."""
    await say(context, chat_id, s,
              "\U0001F517 Link diterima! Bentar, gw bedah dulu isinya...",
              fresh=True)
    stop_bedah = asyncio.Event()
    pmsg_bedah = await context.bot.send_message(
        chat_id, "Lagi gw bedah linknya... 0%" + chr(10) + chr(9617) * 10)
    task_bedah = asyncio.create_task(
        _progress_umum(pmsg_bedah, "Lagi gw bedah linknya", 30, stop_bedah))
    try:
        info = await asyncio.to_thread(bedah_link, url)
    except Exception as e:
        log.exception("bedah link gagal")
        await say(context, chat_id, s,
                  f"\U0001F605 Linknya nggak bisa gw bedah ({e}).\n"
                  "Ketik *deskripsi produknya* manual aja ya "
                  "(bentuk, warna, bahan, fungsi, keunggulan):",
                  parse_mode="Markdown", reply_markup=kb_back_only())
        return MANUAL_DESK
    finally:
        stop_bedah.set()
        try:
            await pmsg_bedah.delete()
        except Exception:
            pass

    if info.get("nama"):
        s["produk"] = info["nama"]
    if info.get("deskripsi"):
        s["deskripsi"] = info["deskripsi"]
    if info.get("selling_points"):
        s["selling_points"] = info["selling_points"]
    if info.get("harga"):
        s["harga"] = info["harga"]

    workdir = _siapkan_workdir(s)
    katalog_path = None
    all_imgs = []
    if info.get("image_url"):
        all_imgs.append(info["image_url"])
    for im in info.get("images", []):
        if im and im not in all_imgs:
            all_imgs.append(im)

    # 1. Coba unduh dari kandidat gambar link produk
    for idx, im_url in enumerate(all_imgs[:6]):
        dest = workdir / f"katalog_link_{idx}.jpg"
        try:
            await asyncio.to_thread(unduh_gambar, im_url, str(dest))
            if dest.exists() and dest.stat().st_size > 2500:
                katalog_path = str(dest)
                break
        except Exception as e:
            log.warning("Gagal unduh gambar link %s: %s", im_url[:60], e)
            continue

    # 2. Fallback: jika gambar dari link nihil/gagal, otomatis cari katalog web
    sumber_katalog = "link"
    if not katalog_path and s.get("produk"):
        try:
            log.info("Gambar dari link nihil, mencari katalog web untuk %s", s["produk"])
            cands = await asyncio.to_thread(cari_kandidat_katalog, s["produk"], s.get("deskripsi", ""), 3)
            for c_idx, c in enumerate(cands[:3]):
                dest = workdir / f"katalog_link_fallback_{c_idx}.jpg"
                try:
                    await asyncio.to_thread(unduh_gambar, c["img"], str(dest))
                    if dest.exists() and dest.stat().st_size > 2500:
                        katalog_path = str(dest)
                        sumber_katalog = "web"
                        break
                except Exception:
                    continue
        except Exception as e:
            log.warning("Fallback cari katalog web gagal: %s", e)

    s["katalog"] = katalog_path
    ringkas = (f"\u2705 *Link kebedah!*\n\n\U0001F4E6 *{s['produk']}*\n"
               + (f"\U0001F4B0 {info['harga']}\n" if info.get("harga") else "")
               + f"\U0001F4DD {s['deskripsi']}\n")
    if s.get("katalog"):
        with open(s["katalog"], "rb") as kf:
            caption_txt = ("\U0001F5BC\ufe0f Gambar produknya udah gw ambil dari linknya \U0001F446"
                           if sumber_katalog == "link" else
                           "\U0001F5BC\ufe0f Gambar produknya udah gw cariin otomatis dari web katalog \U0001F446")
            await context.bot.send_photo(
                chat_id, photo=kf,
                caption=ft(caption_txt))
        ringkas += "\n\U0001F5BC\ufe0f Gambar katalog udah aman \u2014 nanti bisa lo ganti juga di langkah katalog."
    else:
        ringkas += "\n\U0001F5BC\ufe0f Gambarnya belum ketemu di linknya \u2014 nanti pilih katalog di langkah foto ya."
    await say(context, chat_id, s, ringkas + "\n\U0001F3A8 Pilih *style video*:",
              reply_markup=kb_grid(STYLES, "style", back=True),
              parse_mode="Markdown", fresh=True)
    return OPSI_STYLE

async def style_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    s["status_id"] = q.message.message_id
    s["style"] = q.data.split(":")[1]
    teks, kb = _ui_audio(s)
    return await _setelah_pilih(update, context, s, teks, kb, OPSI_AUDIO)


async def audio_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    s["status_id"] = q.message.message_id
    s["audio"] = q.data.split(":")[1]
    teks, kb = _ui_frame(s)
    return await _setelah_pilih(update, context, s, teks, kb, OPSI_FRAME)


async def frame_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    s["status_id"] = q.message.message_id
    s["framing"] = q.data.split(":")[1]
    teks, kb = _ui_durasi(s)
    return await _setelah_pilih(update, context, s, teks, kb, OPSI_DURASI)


async def durasi_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    s["status_id"] = q.message.message_id
    s["durasi"] = int(q.data.split(":")[1])
    if s.pop("dari_konfirmasi", None):
        await _tampilkan_konfirmasi(update, context, s)
        return ConversationHandler.END
    await q.edit_message_text(
        ft(f"\u23f1\ufe0f Durasi: *{s['durasi']} detik*, sip!\n\n" + teks_opsi_foto(s)),
        parse_mode="Markdown", reply_markup=kb_opsi_foto(s))
    return FOTO

def _siapkan_workdir(s: dict) -> Path:
    workdir = WORKDIR / uuid.uuid4().hex[:8]
    workdir.mkdir(parents=True, exist_ok=True)
    s["workdir"] = str(workdir)
    return workdir


async def foto_diterima(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    chat_id = update.effective_chat.id
    if not await _boleh_masuk_produksi(update, context, s, chat_id):
        return None
    workdir = _siapkan_workdir(s)
    photo = update.message.photo[-1]
    f = await context.bot.get_file(photo.file_id)
    path = workdir / "katalog_user.jpg"
    await f.download_to_drive(str(path))
    s["katalog"] = str(path)
    await say(context, chat_id, s, "\U0001F4F8 Foto diterima! Mulai produksi ya, pantau di sini \U0001F447", fresh=True)
    asyncio.create_task(produksi_awal(context, chat_id, s))
    return ConversationHandler.END

async def foto_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol Generate Katalog AI -> tampilkan disclaimer dulu."""
    q = update.callback_query
    uid = q.from_user.id
    aktif = proses_user_aktif(uid)
    if aktif:
        _, pesan = aktif
        try:
            await q.answer("⚠️ Masih ada proses yang belum selesai, tunggu dulu!", show_alert=True)
        except Exception:
            pass
        await say(context, q.message.chat_id, get_session(uid), pesan, fresh=True)
        return
    await q.answer()
    s = get_session(uid)
    s["status_id"] = q.message.message_id
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2705 Lanjut, Generate", callback_data="foto:ai_ok")],
        [InlineKeyboardButton("\u2b05\ufe0f Kembali", callback_data="foto:ai_back")],
    ])
    await q.edit_message_text(ft(DISCLAIMER_AI), parse_mode="Markdown",
                              reply_markup=kb)


async def foto_ai_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User setuju disclaimer -> generate katalog AI."""
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    s["status_id"] = q.message.message_id
    if not await _boleh_masuk_produksi(update, context, s, q.message.chat_id):
        return None
    _siapkan_workdir(s)
    s["katalog"] = None
    asyncio.create_task(produksi_awal(context, q.message.chat_id, s))
    return ConversationHandler.END

async def foto_kembali_opsi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kembali dari disclaimer ke opsi katalog."""
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    s["status_id"] = q.message.message_id
    await q.edit_message_text(ft(teks_opsi_foto(s)), parse_mode="Markdown",
                              reply_markup=kb_opsi_foto(s))


async def opsi_foto_baru(context: ContextTypes.DEFAULT_TYPE, chat_id: int, s: dict):
    """Tampilkan ulang opsi katalog sebagai pesan baru (buat setelah hapus foto kandidat)."""
    await say(context, chat_id, s, teks_opsi_foto(s), parse_mode="Markdown",
              reply_markup=kb_opsi_foto(s), fresh=True)


async def foto_keep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pake katalog yang udah ada dari link -> langsung produksi."""
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    chat_id = q.message.chat_id
    if not await _boleh_masuk_produksi(update, context, s, chat_id):
        return None
    await say(context, chat_id, s,
              "\U0001F4F8 Oke, pake katalog dari link! Mulai produksi ya, pantau di sini \U0001F447",
              fresh=True)
    asyncio.create_task(produksi_awal(context, chat_id, s))
    return ConversationHandler.END

async def foto_web(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cari katalog yang mirip di web (Shopee/Tokopedia/dll)."""
    q = update.callback_query
    uid = q.from_user.id
    aktif = proses_user_aktif(uid)
    if aktif:
        _, pesan = aktif
        try:
            await q.answer("⚠️ Masih ada proses yang belum selesai, tunggu dulu!", show_alert=True)
        except Exception:
            pass
        await say(context, q.message.chat_id, get_session(uid), pesan, fresh=True)
        return
    await q.answer()
    s = get_session(uid)
    chat_id = q.message.chat_id
    s["status_id"] = q.message.message_id
    await q.edit_message_text(
        ft("\U0001F50D Oke, gw cariin katalog yang mirip di web "
        "(Shopee / Tokopedia / TikTok Shop / dll)... bentar ya \u23f3"))
    _siapkan_workdir(s)
    stop_web = asyncio.Event()
    task_web = asyncio.create_task(
        _progress_umum(q, "Lagi gw cariin katalog di web (Shopee/Tokopedia/dll)", 60, stop_web))
    try:
        cands = await asyncio.to_thread(cari_kandidat_katalog,
                                        s["produk"], s["deskripsi"])
    except Exception as e:
        log.exception("cari katalog web gagal")
        await say(context, chat_id, s,
                  f"\U0001F605 Gagal nyari di web ({e}).\n\n" + teks_opsi_foto(s),
                  parse_mode="Markdown", reply_markup=kb_opsi_foto(s))
        return FOTO
    finally:
        stop_web.set()
    s["webcands"] = cands
    s["webidx"] = 0
    s["webvalid_ditolak"] = 0
    if not cands:
        await say(context, chat_id, s,
                  "Yah, nggak nemu yang cocok. Coba upload manual atau generate AI.",
                  parse_mode="Markdown",
                  reply_markup=InlineKeyboardMarkup([[
                      InlineKeyboardButton("Upload Manual", callback_data="webcat:upload"),
                      InlineKeyboardButton("Generate AI", callback_data="foto:ai")]]))
        return FOTO
    await kirim_kandidat_web(context, chat_id, s)


VALIDASI_WEB_MAKS = 5  # maks kandidat yg divalidasi per sesi "Cari Katalog Mirip di Web"


async def kirim_kandidat_web(context: ContextTypes.DEFAULT_TYPE,
                             chat_id: int, s: dict,
                             hapus_msg_id: int | None = None):
    """Unduh & kirim kandidat gambar berikutnya. Tiap kandidat divalidasi AI
    vision; yg ditolak diskip otomatis (maks 5x). Habis/gagal semua -> opsi foto."""
    cands = s.get("webcands", [])
    idx = s.get("webidx", 0)
    ditolak = s.get("webvalid_ditolak", 0)
    dest = None
    while idx < len(cands) and ditolak < VALIDASI_WEB_MAKS:
        dest = Path(s["workdir"]) / f"webcand{idx}.jpg"
        try:
            await asyncio.to_thread(unduh_gambar, cands[idx]["img"], str(dest))
        except Exception:
            log.warning("unduh kandidat %d gagal: %s", idx, cands[idx]["img"][:80])
            idx += 1
            continue
        cocok, alasan = await asyncio.to_thread(
            validasi_kandidat, str(dest), s.get("produk", ""), s.get("deskripsi", ""))
        if cocok:
            break
        ditolak += 1
        log.warning("kandidat web %d ditolak validator (%d/%d): %s",
                    idx, ditolak, VALIDASI_WEB_MAKS, alasan)
        idx += 1
    s["webidx"] = idx
    s["webvalid_ditolak"] = ditolak
    if hapus_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id,
                                             message_id=hapus_msg_id)
        except Exception:
            pass
    if ditolak >= VALIDASI_WEB_MAKS:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Upload Manual", callback_data="webcat:upload")],
            [InlineKeyboardButton("Generate AI", callback_data="foto:ai")],
            [InlineKeyboardButton("Kembali", callback_data="webcat:back")],
        ])
        await say(context, chat_id, s,
                  "Yah, yang ketemu di web nggak ada yang cocok nih. "
                  "Coba upload manual atau generate AI aja.",
                  reply_markup=kb, fresh=True)
        return
    if idx >= len(cands):
        await say(context, chat_id, s,
                  "\U0001F605 Kandidat gambarnya habis / nggak ada yang bisa diunduh.\n\n"
                  + teks_opsi_foto(s),
                  parse_mode="Markdown", reply_markup=kb_opsi_foto(s), fresh=True)
        return
    c = cands[idx]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2705 Gunakan Katalog Ini",
                              callback_data="webcat:use")],
        [InlineKeyboardButton("\U0001F50D Cari Lagi", callback_data="webcat:next")],
        [InlineKeyboardButton("\u274c Batal", callback_data="webcat:cancel"),
         InlineKeyboardButton("\u2b05\ufe0f Kembali", callback_data="webcat:back")],
    ])
    with open(dest, "rb") as df:
        m = await context.bot.send_photo(
            chat_id, photo=df,
            caption=ft(f"\U0001F5BC\ufe0f *Kandidat {idx + 1}/{len(cands)}*\n"
                    f"\U0001F4CD {(c['title'] or '-')[:60]}\n\U0001F517 {c['domain']}"),
            parse_mode="Markdown", reply_markup=kb)
    s["webcand_msg"] = m.message_id


async def webcat_use(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User setuju kandidat -> pake sebagai katalog, lanjut produksi."""
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    chat_id = q.message.chat_id
    if not await _boleh_masuk_produksi(update, context, s, chat_id):
        return None
    idx = s.get("webidx", 0)
    s["katalog"] = str(Path(s["workdir"]) / f"webcand{idx}.jpg")
    try:
        await q.edit_message_caption(caption=ft("\u2705 Katalog dipilih!"),
                                     reply_markup=None)
    except Exception:
        pass
    await say(context, chat_id, s,
              "\U0001F4F8 Katalog dari web diterima! Mulai produksi ya, pantau di sini \U0001F447",
              fresh=True)
    asyncio.create_task(produksi_awal(context, chat_id, s))
    return ConversationHandler.END

async def webcat_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cari lagi -> kandidat berikutnya."""
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    s["webidx"] = s.get("webidx", 0) + 1
    await kirim_kandidat_web(context, q.message.chat_id, s,
                             hapus_msg_id=q.message.message_id)


async def webcat_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback 5x gagal: user pilih Upload Manual -> minta kirim foto langsung."""
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    chat_id = q.message.chat_id
    await say(context, chat_id, s,
        "Silakan upload foto katalog produk langsung ke chat ini.",
        fresh=True)


async def webcat_batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Batal / Kembali dari kandidat web -> opsi katalog."""
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    chat_id = q.message.chat_id
    try:
        await context.bot.delete_message(chat_id=chat_id,
                                         message_id=q.message.message_id)
    except Exception:
        pass
    await opsi_foto_baru(context, chat_id, s)

def teks_pilih_varian(s: dict) -> str:
    n = len(s.get("varian_opsi") or [])
    return ft(chr(0x1F50D) + " Katalognya ada " + str(n) + " varian nih:\n"
              + "Pilih 1 varian biar videonya konsisten ya.")


def kb_pilih_varian(s: dict) -> InlineKeyboardMarkup:
    rows = []
    for i, v in enumerate(s.get("varian_opsi") or []):
        label = (chr(0x1F3A8) + " " + str(v))[:60]
        rows.append([InlineKeyboardButton(label, callback_data="varian:" + str(i))])
    rows.append([InlineKeyboardButton(chr(0x2B05) + " Kembali",
                                     callback_data="varian:kembali")])
    return InlineKeyboardMarkup(rows)


def _deskripsi_lock(s: dict) -> str:
    # PRODUCT_LOCK: jika user memilih varian, deskripsi varian terpilih
    # dipakai di semua scene (bukan deskripsi umum).
    return s.get("varian_terpilih") or s.get("deskripsi") or ""


async def _deteksi_varian_katalog(context, chat_id: int, s: dict) -> bool:
    """Deteksi multi-varian katalog SEBELUM enhance HD.

    True  -> katalog multi-varian; pesan pilihan varian sudah dikirim,
             pemanggil harus menunggu user memilih (s['varian_event']).
    False -> lanjut seperti biasa (single-varian / deteksi gagal).
    """
    s.pop("varian_opsi", None)
    s.pop("varian_terpilih", None)
    s.pop("varian_event", None)
    try:
        hasil = await asyncio.to_thread(analyze_catalog_variants, s["katalog"])
    except Exception:
        return False
    varian = (hasil or {}).get("varian") or []
    if not (hasil or {}).get("multi") or len(varian) < 2:
        return False
    s["varian_opsi"] = varian
    await context.bot.send_message(
        chat_id=chat_id,
        text=teks_pilih_varian(s),
        reply_markup=kb_pilih_varian(s),
    )
    return True


async def varian_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User memilih 1 varian katalog -> lock ke varian itu, lanjut produksi."""
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    s = sessions.get(uid)
    data = q.data or ""
    ev = (s or {}).get("varian_event")
    if s is None or ev is None or not s.get("varian_opsi"):
        await q.answer("Pilihannya sudah kedaluwarsa, mulai lagi dari /menu ya.",
                       show_alert=True)
        return
    if data == "varian:kembali":
        s.pop("varian_opsi", None)
        s.pop("varian_terpilih", None)
        try:
            await q.edit_message_text(ft(
                chr(0x2B05) + " Oke, pakai seluruh katalog ya.\n"
                + "Lanjut produksi..."))
        except Exception:
            pass
        ev.set()
        return
    try:
        dipilih = s["varian_opsi"][int(data.split(":", 1)[1])]
    except (ValueError, IndexError, TypeError):
        await q.answer("Pilihan tidak valid.", show_alert=True)
        return
    s["varian_terpilih"] = dipilih
    s.pop("varian_opsi", None)
    try:
        await q.edit_message_text(ft(
            chr(0x2705) + " Varian dipilih: " + str(dipilih) + "\n"
            + "Video dikunci ke varian ini di semua scene. Lanjut produksi..."))
    except Exception:
        pass
    ev.set()


async def produksi_awal(context, chat_id: int, s: dict):
    workdir = Path(s["workdir"])
    flow = FlowClient(FLOW_BASE, FLOW_KEY)
    s["is_producing_storyboard"] = True
    s["produksi_task"] = asyncio.current_task()
    async with job_lock:
        try:
            if not s.get("katalog"):
                await say(context, chat_id, s,
                          progress_ts("Lagi bikin gambar katalog dulu..."))
                img = None
                try:
                    img = await flow.generate_image(
                        catalog_prompt(s["produk"], s["deskripsi"]), "9:16")
                finally:
                    if img is None:
                        await _tandai_progress_gagal(context, chat_id, s, "katalog")
                p = workdir / "katalog.jpg"
                p.write_bytes(img)
                s["katalog"] = str(p)
            with open(s["katalog"], "rb") as kf:
                await context.bot.send_photo(chat_id, photo=kf,
                                             caption=ft("Gambar katalognya nih"))

            # Deteksi multi-varian katalog SEBELUM enhance HD.
            # Single-varian / deteksi gagal -> lanjut seperti biasa.
            if await _deteksi_varian_katalog(context, chat_id, s):
                ev = asyncio.Event()
                s["varian_event"] = ev
                try:
                    await asyncio.wait_for(ev.wait(), timeout=600)
                except (asyncio.TimeoutError, TimeoutError):
                    pass  # lanjut sebagai single-varian
                finally:
                    s.pop("varian_event", None)

            await say(context, chat_id, s,
                      "Lagi gw enhance ke HD - produk dibikin tajam, yang nggak penting dibuang...", fresh=True)
            stop_enh = asyncio.Event()
            pmsg_enh = await context.bot.send_message(
                chat_id, "Lagi gw enhance ke HD... 0%" + chr(10) + chr(9617) * 10)
            task_enh = asyncio.create_task(
                _progress_umum(pmsg_enh, "Lagi gw enhance ke HD", 60, stop_enh))
            try:
                            # Kunci varian: fokus enhance hanya pada varian terpilih bila ada.
                            enhance_prompt = ENHANCE_PROMPT
                            if s.get("varian_terpilih"):
                                enhance_prompt += (
                                    "\n\nFOCUS TARGET: Enhance ONLY the product variant: "
                                    + str(s["varian_terpilih"])
                                    + ". Isolate and focus on this specific variant, ignoring other items in the original image."
                                )

                            img = await flow.generate_image(enhance_prompt, "9:16",
                                                            ref_paths=[s["katalog"]])
            finally:
                stop_enh.set()
                try:
                    await pmsg_enh.delete()
                except Exception:
                    pass
            p = workdir / "katalog_hd.jpg"
            p.write_bytes(img)
            s["katalog_hd"] = str(p)
            with open(p, "rb") as hdf:
                await context.bot.send_photo(chat_id, photo=hdf,
                                             caption=ft("Katalog HD-nya, lebih tajam"))

            await say(context, chat_id, s, "Lagi nyusun storyboard...", fresh=True)
            stop_sb = asyncio.Event()
            pmsg_sb = await context.bot.send_message(
                chat_id, "Lagi gw susun storyboard-nya... 0%" + chr(10) + chr(9617) * 10)
            task_sb = asyncio.create_task(
                _progress_umum(pmsg_sb, "Lagi gw susun storyboard-nya", 40, stop_sb))
            try:
                            board = await generate_storyboard(
                                s["produk"], _deskripsi_lock(s), s["durasi"],
                    s["style"], s["audio"], s["framing"],
                    katalog_path=s.get("katalog_hd") or s.get("katalog"))
            finally:
                stop_sb.set()
                try:
                    await pmsg_sb.delete()
                except Exception:
                    pass
            s["board"] = board
            scenes = board["scenes"]

            frames = []
            for i, sc in enumerate(scenes):
                await say(context, chat_id, s,
                          progress_ts(f"Lagi bikin frame {i+1}/{len(scenes)} - {sc['judul']}..."))
                prompt = build_frame_prompt(
                    sc,
                    spec_text=board.get("product_spec_text", ""),
                    style=s.get("style", "sinematik"),
                    framing=s.get("framing", "tangan"),
                    produk=s.get("produk", ""),
                    character=board.get("character_lock", ""),
                )
                img = None
                try:
                    img = await flow.generate_image(prompt, "9:16",
                    ref_paths=[s["katalog_hd"]])
                finally:
                    if img is None:
                        await _tandai_progress_gagal(context, chat_id, s, "frame")
                fp = workdir / f"frame{i+1}.jpg"
                fp.write_bytes(img)
                frames.append(str(fp))
            s["frames"] = frames

            sheet_path = workdir / "storyboard_sheet.jpg"
            hero_katalog = s.get("katalog_hd") or s.get("katalog")
            compose_sheet(board, s["produk"], "Promosi", s["durasi"],
                          hero_katalog, frames, str(sheet_path),
                          style=s.get("style", "sinematik"), audio=s.get("audio", "vo"))
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001F680 Gas Bikin Video", callback_data="gas:video")],
                [InlineKeyboardButton("\u274c Batal", callback_data="gas:batal")],
            ])
            with open(sheet_path, "rb") as sf:
                await context.bot.send_photo(
                    chat_id, photo=sf,
                    caption=ft(f"Storyboard: {board['title']}\nCek dulu ya - kalau oke, tap Gas Bikin Video."),
                    reply_markup=kb)
            await say(context, chat_id, s,
                      "Storyboard-nya udah jadi (di atas). Kalau oke, tap tombol Gas Bikin Video di sheet-nya ya.", fresh=True)
        except Exception as e:
            log.exception("produksi_awal gagal")
            kb_gagal = InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001F504 Coba Lagi", callback_data="gas:retry")],
                [InlineKeyboardButton("\U0001F3E0 Menu Utama", callback_data="gas:menu")],
            ])
            await say(context, chat_id, s,
                      "\u26a0\ufe0f Waduh, gagal di tengah jalan (timed out).\n\n"
                      "Biasanya karena server AI lagi lambat. Mau coba lagi?",
                      reply_markup=kb_gagal)
        finally:
            s["is_producing_storyboard"] = False
            s["produksi_task"] = None


async def gas_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = sessions.get(q.from_user.id)
    if not s:
        return
    t = s.get("video_task")
    if t and not t.done():
        t.cancel()
    try:
        await context.bot.edit_message_text(
            ft("Lagi dibatalkan..."),
            chat_id=q.message.chat_id, message_id=q.message.message_id)
    except Exception:
        pass


async def _progress_updater(q, expected_sec, stop_event):
    import time
    start = time.time()
    expected_sec = max(20, int(expected_sec or 30))
    while not stop_event.is_set():
        await asyncio.sleep(15)
        if stop_event.is_set():
            break
        elapsed = time.time() - start
        pct = min(92, int(elapsed / expected_sec * 100))
        bar = chr(9608) * (pct // 10) + chr(9617) * (10 - pct // 10)
        sisa = max(5, expected_sec - int(elapsed))
        teks = (
            f"🔔 *Antrean Terpenuhi — Sedang Memproses Video!*\n\n"
            f"🎬 Progress pembuatan video: *{pct}%*\n{bar}\n\n"
            f"⏳ Estimasi sisa waktu: ~{sisa} detik lagi..."
        )
        try:
            await q.edit_message_text(ft(teks))
        except Exception:
            pass


async def _progress_umum(q, pesan_awal, estimasi_detik, stop_event, interval_detik=15):
    """Progress indicator generik utk operasi lama non-video (riset produk,
    cari katalog web, enhance HD, storyboard, bedah link). Mirip _progress_updater:
    bar + persen, update tiap interval_detik detik (default 15), maks 90%.
    q = CallbackQuery (edit_message_text) atau Message (edit_text).
    Tanpa ft(): pesan progress dihapus/diedit setelah operasi selesai."""
    import time
    start = time.time()
    while not stop_event.is_set():
        await asyncio.sleep(interval_detik)
        if stop_event.is_set():
            break
        elapsed = time.time() - start
        pct = min(90, int(elapsed / estimasi_detik * 100))
        bar = chr(9608) * (pct // 10) + chr(9617) * (10 - pct // 10)
        teks = pesan_awal + " ... " + str(pct) + "%" + chr(10) + bar
        try:
            edit = getattr(q, "edit_message_text", None)
            if edit is None:
                edit = getattr(q, "edit_text", None)
            if edit is not None:
                await edit(teks)
        except Exception:
            pass


async def gas_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """FASE 2: setelah user approve sheet -> pilih tier -> masuk antrean.
    Worker (antrean_worker) yang generate satu per satu."""
    q = update.callback_query
    uid = q.from_user.id
    chat_id = q.message.chat_id
    s = sessions.get(uid)
    if await tolak_kalau_ban(update, context):
        return
    if q.data == "gas:menu":
        await q.answer()
        return await menu_utama_cb(update, context)
    if q.data == "gas:batal" or not s or not s.get("frames"):
        await q.answer()
        try:
            await q.edit_message_caption(caption=ft("\u274c Dibatalkan."))
        except Exception:
            pass
        return
    if q.data == "gas:batal_antri":
        await q.answer()
        if antrean.batal_antrean(uid):
            try:
                await q.message.delete()
            except Exception:
                pass
            await say(context, chat_id, s or {}, "❌ Antreanmu berhasil dibatalkan.", fresh=True)
            asyncio.create_task(_update_posisi_antrean_semua(context.application))
        else:
            await say(context, chat_id, s or {},
                      "⚠️ Antreanmu sudah mulai diproses, tidak bisa dibatalkan.",
                      fresh=True)
        return
    if q.data == "gas:edit":
        await q.answer()
        await _tampilkan_menu_edit(update, context, s)
        return
    if q.data == "gas:kembali":
        await q.answer()
        await _tampilkan_konfirmasi(update, context, s)
        return

    # AKSI GENERATE / SUBMIT / RETRY:
    # 1. Anti double-click / klik berulang kali
    if is_spam_click(uid, 2.0):
        try:
            await q.answer("⚠️ Tombol sudah ditekan sebelumnya, mohon tunggu sebentar...", show_alert=True)
        except Exception:
            pass
        return

    # 2. Cek apakah masih ada proses aktif yang sedang berjalan
    aktif = proses_user_aktif(uid)
    if aktif:
        tipe, pesan = aktif
        popup_pesan = "⚠️ Masih ada proses yang belum selesai, tunggu dulu!"
        if tipe in ("video_generating", "antrean_waiting"):
            pos = antrean.posisi_antrean(uid)
            pos_txt = f" (Posisi antrean #{pos})" if pos > 0 else ""
            popup_pesan = f"⚠️ Masih ada video yang sedang diproses{pos_txt}! Tunggu dulu sampai selesai ya."
        elif tipe == "storyboard_producing":
            popup_pesan = "⚠️ Masih ada proses storyboard yang sedang berjalan! Tunggu sebentar ya."
        try:
            await q.answer(popup_pesan, show_alert=True)
        except Exception:
            pass
        await say(context, chat_id, s or {}, pesan, fresh=True)
        return

    user_lock = get_user_lock(uid)
    if user_lock.locked():
        try:
            await q.answer("⚠️ Masih ada proses yang belum selesai, tunggu dulu!", show_alert=True)
        except Exception:
            pass
        return

    async with user_lock:
        if q.data == "gas:retry":
            await q.answer("Siap, coba lagi...")
            if not s or not s.get("board") or not s.get("katalog_hd"):
                await say(context, chat_id, s or {},
                          "\u26a0\ufe0f Sesi produksi sudah tidak valid. "
                          "Mulai lagi dari Menu Utama ya.",
                          reply_markup=InlineKeyboardMarkup(
                              [[InlineKeyboardButton("\U0001F3E0 Menu Utama",
                                                     callback_data="gas:menu")]]))
                return
            await say(context, chat_id, s,
                      "\U0001F504 Oke, coba lagi dari awal ya...", fresh=True)
            asyncio.create_task(produksi_awal(context, chat_id, s))
            return

        if q.data == "gas:video":
            await q.answer()
            await _tampilkan_konfirmasi(update, context, s)
            return
        if q.data.startswith("gas:tier:"):
            await q.answer()
            tier = q.data.split(":")[2]
            _seg = max(1, int(s.get("durasi", 8)) // 8)
            if not(is_admin(uid) or billing.is_unlimited(uid)):
                if billing.get_saldo(uid)<billing.harga_video_segmen(uid,tier,_seg):
                    kb=InlineKeyboardMarkup([
                    [InlineKeyboardButton(chr(0x1F4B3)+" Topup",callback_data="menu:topup")],
                    [InlineKeyboardButton(chr(0x1F3E0)+" Menu",callback_data="menu:utama")]])
                    await q.edit_message_text(chr(0x1F4B0)+" Saldo kurang.",reply_markup=kb)
                    return
            await _gas_masuk_antrean(context, chat_id, s, uid, tier)
            return
        if q.data == "gas:go":
            await q.answer()
            # "gas:go": admin / unlimited langsung antre prioritas tanpa pilih tier
            # MASTER SWITCH: mode non-komersial -> skip pilih tier dan antrean.
            if not billing.komersial_aktif():
                await _gas_masuk_antrean(context, chat_id, s, uid, "reguler", gratis=True)
                return
            if is_admin(uid) or billing.is_unlimited(uid):
                await _gas_masuk_antrean(context, chat_id, s, uid, "prioritas",
                                         gratis=True)
                return
            _segmen = max(1, int(s.get("durasi", 8)) // 8)
            kb_tier = InlineKeyboardMarkup([
                [InlineKeyboardButton(chr(0x1F4E6)+" %s - Reguler" % billing.rp(billing.harga_video_segmen(uid, "reguler", _segmen)),
                                      callback_data="gas:tier:reguler")],
                [InlineKeyboardButton(chr(0x26A1)+" %s - Prioritas" % billing.rp(billing.harga_video_segmen(uid, "prioritas", _segmen)),
                                      callback_data="gas:tier:prioritas")],
            ])
            await say(context, chat_id, s,
                      "Pilih tier generate:\n"
                      "Reguler = antre biasa.\n"
                      "Prioritas = dikerjakan duluan.",
                      reply_markup=kb_tier, fresh=True)
            return




class _QShim:
    """Pengganti callback query untuk _progress_updater di worker antrean."""

    def __init__(self, bot, chat_id, message_id, reply_markup=None):
        self._bot = bot
        self._chat_id = chat_id
        self._message_id = message_id
        self._reply_markup = reply_markup

    async def edit_message_text(self, text, reply_markup=None):
        rm = reply_markup if reply_markup is not None else self._reply_markup
        await self._bot.edit_message_text(
            chat_id=self._chat_id,
            message_id=self._message_id,
            text=text,
            reply_markup=rm,
            parse_mode="Markdown",
        )


async def _update_posisi_antrean_semua(app):
    """Update pesan antrean untuk semua user yang masih menunggu di antrean."""
    try:
        q = antrean._load()
        for idx, item in enumerate(q):
            uid = item.get("uid")
            jd = item.get("job_data") or {}
            chat_id = jd.get("chat_id") or uid
            msg_id = jd.get("antrean_msg_id")
            if not msg_id or not chat_id:
                continue
            pos = idx + (2 if antrean.sedang_diproses() else 1)
            est = pos * 3
            kb_batal = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Batalkan Antrian", callback_data="gas:batal_antri")]
            ])
            teks = (
                f"⏳ *Update Antrean*\n\n"
                f"Antrean di depanmu sudah selesai diproses!\n"
                f"Posisi kamu sekarang: *#{pos}*\n"
                f"Estimasi waktu: *~{est} menit*\n\n"
                f"Mohon tunggu ya, giliranmu segera tiba! 🚀"
            )
            try:
                await app.bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=int(msg_id),
                    text=ft(teks),
                    parse_mode="Markdown",
                    reply_markup=kb_batal,
                )
            except Exception:
                pass
    except Exception as e:
        log.warning("_update_posisi_antrean_semua gagal: %s", e)


async def antrean_worker(app):
    """Worker FASE 2: ambil job terdepan satu per satu, generate, kirim hasil,
    potong saldo SETELAH sukses. Satu sesi dalam satu waktu (job_lock)."""
    log.info("Antrean worker jalan.")
    while True:
        try:
            job = antrean.ambil_berikutnya()
        except Exception:
            log.exception("ambil_berikutnya gagal")
            job = None
        if not job:
            await asyncio.sleep(5)
            continue
        try:
            async with job_lock:
                proc = asyncio.create_task(_proses_antrean(app, job))
                try:
                    sess = sessions.get(int(job["uid"]))
                except (ValueError, TypeError):
                    sess = None
                if isinstance(sess, dict):
                    sess["video_task"] = proc
                try:
                    await proc
                except asyncio.CancelledError:
                    if proc.cancelled():
                        log.info("Job antrean %s dibatalkan.", job.get("uid"))
                    else:
                        raise
        finally:
            antrean.selesai_diproses()


async def _start_worker(app):
    app.create_task(antrean_worker(app))


async def _proses_antrean(app, job):
    uid = int(job["uid"])
    jd = job.get("job_data") or {}
    chat_id = int(jd.get("chat_id") or uid)
    bill_harga = int(jd.get("bill_harga") or 0)
    s = dict(jd)
    s["status_id"] = None
    sess = sessions.get(uid)
    if isinstance(sess, dict):
        sess["is_generating_video"] = True
    s["is_generating_video"] = True
    if not antrean.sedang_diproses():
        antrean._sedang_diproses = job
    workdir = Path(s["workdir"])
    flow = FlowClient(FLOW_BASE, FLOW_KEY)
    stop_prog = None
    prog_task = None
    prog_msg = None
    try:
        # 1. HAPUS PESAN ANTREAN LAMA DARI CHAT
        old_msg_ids = set()
        if jd.get("antrean_msg_id"):
            old_msg_ids.add(jd["antrean_msg_id"])
        if s.get("antrean_msg_id"):
            old_msg_ids.add(s["antrean_msg_id"])
        if s.get("status_id"):
            old_msg_ids.add(s["status_id"])
        for mid in jd.get("pesan_lama_ids", []):
            if mid:
                old_msg_ids.add(mid)
        if isinstance(sess, dict):
            if sess.get("antrean_msg_id"):
                old_msg_ids.add(sess["antrean_msg_id"])
            if sess.get("status_id"):
                old_msg_ids.add(sess["status_id"])

        for mid in old_msg_ids:
            try:
                await app.bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass

        # Beritahu / update semua antrean yang masih menunggu di belakang
        await _update_posisi_antrean_semua(app)

        # 2. KIRIM PEMBERITAHUAN RESMI: ANTREAN TERPENUHI & GILIRAN TIBA
        kb_stop = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Batal Bikin Video", callback_data="gas:stop")
        ]])
        teks_mulai = (
            "🔔 *Antreanmu Terpenuhi!*\n\n"
            "Giliran kamu sudah tiba! 🚀\n"
            "Video sedang diproduksi dari storyboard...\n"
            "Mohon tunggu sebentar ya 🙏"
        )
        prog_msg = await app.bot.send_message(
            chat_id=chat_id,
            text=ft(teks_mulai),
            parse_mode="Markdown",
            reply_markup=kb_stop,
        )
        s["status_id"] = prog_msg.message_id
        if isinstance(sess, dict):
            sess["video_task"] = asyncio.current_task()
            sess["status_id"] = prog_msg.message_id

        q = _QShim(app.bot, chat_id, prog_msg.message_id, reply_markup=kb_stop)
        board = s["board"]
        vprompt = build_video_prompt(board, s["produk"], s["durasi"],
                                     s["style"], s["audio"], s["framing"])
        frames = s["frames"]
        stop_prog = asyncio.Event()
        prog_task = asyncio.create_task(
            _progress_updater(q, s.get("durasi"), stop_prog))
        try:
            try:
                mp4 = await flow.generate_video(vprompt, frames,
                                                seconds=s["durasi"], aspect="9:16",
                                            reference_images=[s["katalog_hd"]] if s.get("katalog_hd") else None)
            except Exception:
                # fallback: frame pertama saja
                mp4 = await flow.generate_video(vprompt, frames[:1],
                                                seconds=s["durasi"], aspect="9:16",
                                            reference_images=[s["katalog_hd"]] if s.get("katalog_hd") else None)
            stop_prog.set()
            if prog_task is not None:
                prog_task.cancel()

            out = workdir / "final.mp4"
            out.write_bytes(mp4)
            s["video_task"] = None
            if isinstance(sess, dict):
                sess["video_task"] = None

            # Hapus pesan progress/status antrean agar chat bersih
            if prog_msg:
                try:
                    await app.bot.delete_message(chat_id=chat_id, message_id=prog_msg.message_id)
                except Exception:
                    pass
            s["status_id"] = None
            if isinstance(sess, dict):
                sess["status_id"] = None

            # Kirim pemberitahuan resmi bahwa antrean sukses terpenuhi & video jadi
            await app.bot.send_message(
                chat_id=chat_id,
                text=ft(
                    "🎉 *Antrean Sukses Terpenuhi!*\n\n"
                    "Video promosi produkmu sudah selesai dan siap digunakan! 🚀\n"
                    "Hasil video ada di bawah ini 👇"
                ),
                parse_mode="Markdown",
            )
            with open(out, "rb") as vf:
                await app.bot.send_video(chat_id, video=vf,
                                         caption=ft("\U0001F3AC " + board["title"]))
            billing.catat_generate(uid)  # FASE 3: catat video sukses
            billing.cairkan_bonus_referal(uid)  # FASE 4: cairkan bonus referal pending (video pertama)
            billing.catat_generate_user(uid)  # FASE 4: hitung video sukses per user
            if bill_harga > 0:
                billing.clear_discount_pct(uid)
                ok = billing.kurang_saldo(uid, bill_harga)
                if ok is False:
                    await say(app, chat_id, s,
                              "Maaf, saldo tidak cukup buat video ini.")
                else:
                    await say(app, chat_id, s,
                              "Rp %s kepotong buat video ini. Sisa saldo: Rp %s."
                              % (billing.rp(bill_harga), billing.rp(billing.get_saldo(uid))))
            if s.get("audio") in ("vo", "vo_id", "vo_en"):
                scenes = board.get("scenes", [])
                seg = max(1, s["durasi"]) // max(1, len(scenes))
                lines, t = [], 0
                for i, sc in enumerate(scenes):
                    t2 = s["durasi"] if i == len(scenes) - 1 else t + seg
                    nar = sc.get("narasi", "").strip()
                    if nar:
                        lines.append(f"• Beat {i+1} ({t}-{t2}s): \"{nar}\"")
                    t = t2
                full = " ".join(sc["narasi"].strip() for sc in scenes if sc.get("narasi", "").strip())
                if lines:
                    await app.bot.send_message(
                        chat_id,
                        ft("🗣️ *Skrip Narasi / Voiceover Video (Tersinkronisasi)*\n\n"
                           "Skrip berikut sudah disinkronkan langsung ke dalam video di atas:\n\n"
                           "📋 *Teks Lengkap:*\n"
                           f"```\n{full}\n```\n\n"
                           "⏱️ *Rincian per Beat Scene:*\n" + "\n".join(lines) + "\n\n"
                           "💡 *Tip:* Kamu juga bisa gunakan teks di atas untuk auto-caption / subtitle di CapCut atau TikTok!"),
                        parse_mode="Markdown")
            elif s.get("audio") == "asmr_vo":
                scenes = board["scenes"]
                seg = max(1, s["durasi"]) // max(1, len(scenes))
                lines, t = [], 0
                for i, sc in enumerate(scenes):
                    t2 = s["durasi"] if i == len(scenes) - 1 else t + seg
                    if sc.get("narasi"):
                        lines.append("Scene %d (%d-%d dtk): %s" % (i + 1, t, t2, sc["narasi"]))
                    t = t2
                full = " ".join(sc["narasi"] for sc in scenes if sc.get("narasi"))
                await app.bot.send_message(
                    chat_id,
                    ft("\U0001F399\ufe0f *Skrip VO buat ElevenLabs*\n\n"
                       "Video di atas suaranya ASMR aja \u2014 VO-nya lo pasang manual "
                       "pas edit, pake skrip ini:\n\n"
                       "\U0001F4CB *Teks lengkap* \u2014 tinggal copy-paste ke ElevenLabs:\n"
                       "```\n" + full + "\n```\n\n"
                       "\u23f1\ufe0f *Rincian per scene:*\n" + "\n".join(lines) + "\n\n"
                       "Cara pakai: buka elevenlabs.io \u2192 Text to Speech \u2192 paste teks "
                       "lengkapnya \u2192 pilih voice Bahasa Indonesia \u2192 generate \u2192 "
                       "download MP3-nya \u2192 gabungin ke video pas ngedit (timing-nya "
                       "ikut rincian per scene di atas)."),
                    parse_mode="Markdown")
            # Caption + hashtag: riset dulu dari tren terkini, kirim di tiap video jadi
            await say(app, chat_id, s,
                      "\U0001F4DD Videonya udah jadi! Bentar, gw risetin caption + hashtag yang pas dulu...")
            try:
                ch = await asyncio.to_thread(riset_caption, s["produk"],
                                             s["deskripsi"])
                isi = ch["caption"] + "\n\n" + " ".join(ch["hashtags"])
                await app.bot.send_message(
                    chat_id,
                    ft("\U0001F4DD *Caption + hashtag siap posting*\n"
                       "Tinggal copy-paste ke TikTok ya \U0001F447\n"
                       "\n```\n" + isi + "\n```\n"),
                    parse_mode="Markdown")
                await say(app, chat_id, s,
                          "\u2705 Beres semua! Videonya di atas \U0001F446 caption + hashtag tinggal copy. "
                          "Mau bikin lagi? /start aja \U0001F680")
            except Exception:
                log.exception("riset caption gagal")
                await say(app, chat_id, s,
                          "\U0001F605 Caption-nya gagal gw riset, tapi videonya udah jadi di atas \U0001F446")

            # Update posisi antrean user lain setelah selesai
            await _update_posisi_antrean_semua(app)

        except asyncio.CancelledError:
            s["video_task"] = None
            if prog_msg:
                try:
                    await app.bot.delete_message(chat_id=chat_id, message_id=prog_msg.message_id)
                except Exception:
                    pass
            await say(app, chat_id, s, "\u274c\ufe0f Pembuatan video udah dibatalkan.", fresh=True)
            await _update_posisi_antrean_semua(app)
        except Exception as e:
            log.exception("antrean gagal")
            if prog_msg:
                try:
                    await app.bot.delete_message(chat_id=chat_id, message_id=prog_msg.message_id)
                except Exception:
                    pass
            kb_gagal_video = InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001F504 Coba Lagi", callback_data="gas:retry")],
                [InlineKeyboardButton("\U0001F3E0 Menu Utama", callback_data="gas:menu")],
            ])
            await say(app, chat_id, s,
                      f"\U0001F605 Waduh, videonya gagal dibikin: {e}",
                      reply_markup=kb_gagal_video, fresh=True)
            await _update_posisi_antrean_semua(app)
    finally:
        antrean.selesai_diproses()
        if isinstance(sess, dict):
            sess["video_task"] = None
            sess["is_generating_video"] = False
        s["is_generating_video"] = False
        if stop_prog is not None:
            stop_prog.set()
        if prog_task is not None:
            prog_task.cancel()

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(ft("Oke, dibatalkan. /start lagi kalau mau mulai dari awal ya."))
    return ConversationHandler.END


async def _ke_menu(q, s):
    s["status_id"] = q.message.message_id
    await q.edit_message_text(ft(MENU_TEXT), reply_markup=kb_menu(q.from_user.id))
    return MENU


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    return await _ke_menu(q, get_session(q.from_user.id))


async def back_dari_produk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    s["status_id"] = q.message.message_id
    await q.edit_message_text(
        ft("Riset Otomatis\n\nMau riset kategori apa nih? Ketik aja kategorinya "
        "(misal: dapur, skincare, gadget) atau ketik bebas biar gw yang mikir:"),
        reply_markup=kb_back_only())
    return RISET_KAT


async def back_dari_desk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    s["status_id"] = q.message.message_id
    await q.edit_message_text(ft("Mode Manual\n\nSiap! Kirim nama produk yang mau lo buat videonya:"),
                              reply_markup=kb_back_only())
    return MANUAL_NAMA


async def back_dari_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    s["status_id"] = q.message.message_id
    if s.pop("dari_konfirmasi", None):
        await _tampilkan_konfirmasi(update, context, s)
        return ConversationHandler.END
    if s.get("kandidat"):
        btns = [[InlineKeyboardButton(f"{i+1}. {k['nama'][:40]}", callback_data=f"prod:{i}")]
                for i, k in enumerate(s["kandidat"])]
        btns.append([InlineKeyboardButton("\u2b05\ufe0f Kembali", callback_data="nav:back")])
        teks = "Hasil riset - produk yang berpotensi rame:\n"
        for i, k in enumerate(s["kandidat"]):
            teks += f"\n{i+1}. {k['nama']} ({k.get('harga_kisaran','')})\n{k.get('alasan','')}"
        teks += "\n\nTap produk yang lo mau ya:"
        await q.edit_message_text(ft(teks), reply_markup=InlineKeyboardMarkup(btns))
        return PILIH_PRODUK
    return await _ke_menu(q, s)


async def back_dari_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    s["status_id"] = q.message.message_id
    if s.pop("dari_konfirmasi", None):
        await _tampilkan_konfirmasi(update, context, s)
        return ConversationHandler.END
    teks, kb = _ui_style(s)
    await q.edit_message_text(teks, reply_markup=kb)
    return OPSI_STYLE


async def back_dari_frame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    s["status_id"] = q.message.message_id
    if s.pop("dari_konfirmasi", None):
        await _tampilkan_konfirmasi(update, context, s)
        return ConversationHandler.END
    teks, kb = _ui_audio(s)
    await q.edit_message_text(teks, reply_markup=kb)
    return OPSI_AUDIO


async def back_dari_durasi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    s["status_id"] = q.message.message_id
    if s.pop("dari_konfirmasi", None):
        await _tampilkan_konfirmasi(update, context, s)
        return ConversationHandler.END
    teks, kb = _ui_frame(s)
    await q.edit_message_text(teks, reply_markup=kb)
    return OPSI_FRAME


async def back_dari_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(q.from_user.id)
    s["status_id"] = q.message.message_id
    teks, kb = _ui_durasi(s)
    await q.edit_message_text(teks, reply_markup=kb)
    return OPSI_DURASI




# ==================== BILLING ====================

async def saldo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _tolak_jika_komersial_nonaktif(update, context):
        return
    uid = update.effective_user.id
    saldo = billing.get_saldo(uid)
    harga = billing.harga_video()
    if is_admin(uid):
        teks = ("Dompet kamu (admin, gratis selamanya):\n"
                "Saldo: Rp %s\n"
                "Harga per video: Rp %s" % (billing.rp(saldo), billing.rp(harga)))
    else:
        teks = ("Dompet kamu:\n"
                "Saldo: Rp %s\n"
                "Harga per video: Rp %s" % (billing.rp(saldo), billing.rp(harga)))
        if billing.is_unlimited(uid):
            teks += "\nUnlimited: aktif, sisa %s" % billing.fmt_sisa(billing.unlimited_sisa(uid))
        pct = billing.get_discount_pct(uid)
        if pct:
            teks += "\nDiskon %d persen untuk 1x generate berikutnya" % pct
    await _balas_menu(update, teks, reply_markup=kb_kembali_utama())


async def redeem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _tolak_jika_komersial_nonaktif(update, context):
        return
    if not context.args:
        await update.message.reply_text(ft("Pakai: /redeem <kode>"))
        return
    ok, pesan = billing.redeem_voucher(update.effective_user.id, context.args[0])
    await update.message.reply_text(ft(pesan))


TOPUP_NOMINALS = (10000, 25000, 50000, 100000)


def _kb_topup_nominal():
    rows = [[InlineKeyboardButton("Rp %s" % billing.rp(n), callback_data="topup:nom:%d" % n)]
            for n in TOPUP_NOMINALS]
    rows.append([InlineKeyboardButton("Nominal lain", callback_data="topup:nom:custom")])
    rows.append([InlineKeyboardButton("\u274c Batal", callback_data="topup:batal")])
    return InlineKeyboardMarkup(rows)


def _kb_topup_bayar():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2705 Sudah Bayar", callback_data="topup:bayar")],
        [InlineKeyboardButton("\u274c Batal", callback_data="topup:batal")],
    ])


async def _topup_tampil_bayar(q, context, uid, amount):
    context.user_data["topup_amount"] = amount
    teks = ("Topup Rp %s\n\n%s\n\nKalau udah transfer, tekan Sudah Bayar."
            % (billing.rp(amount), billing.get_payment_info()))
    if context.user_data.get("topup_dari_menu"):
        context.user_data["menu_kembali_ke"] = "menu:topup"
    kb = _kb_topup_bayar_menu() if context.user_data.get("topup_dari_menu") else _kb_topup_bayar()
    await q.edit_message_text(ft(teks), reply_markup=kb)
    return TOPUP_BAYAR


async def topup_mulai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _tolak_jika_komersial_nonaktif(update, context):
        return
    context.user_data["topup_dari_menu"] = False
    if is_admin(update.effective_user.id):
        await update.message.reply_text(ft("Kamu admin, gratis terus. Nggak perlu topup."))
        return ConversationHandler.END
    await update.message.reply_text(ft("Mau topup berapa? Pilih nominal:"),
                                    reply_markup=_kb_topup_nominal())
    return TOPUP_NOMINAL


async def topup_nominal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "topup:batal":
        await q.edit_message_text(ft("Topup dibatalkan."))
        return ConversationHandler.END
    val = q.data.rsplit(":", 1)[-1]
    if val == "custom":
        await q.edit_message_text(ft("Ketik nominal topup (angka rupiah, misal 15000):"))
        return TOPUP_CUSTOM
    return await _topup_tampil_bayar(q, context, q.from_user.id, int(val))


async def topup_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip().replace(".", "").replace(",", "")
    amount = int(raw) if raw.isdigit() else 0
    if amount < billing.min_topup():
        await update.message.reply_text(ft("Nominal minimal Rp %s. Ketik lagi atau /cancel." % billing.rp(billing.min_topup())))
        return TOPUP_CUSTOM
    context.user_data["topup_amount"] = amount
    teks = ("Topup Rp %s\n\n%s\n\nKalau udah transfer, tekan Sudah Bayar."
            % (billing.rp(amount), billing.get_payment_info()))
    if context.user_data.get("topup_dari_menu"):
        context.user_data["menu_kembali_ke"] = "menu:topup"
    kb = _kb_topup_bayar_menu() if context.user_data.get("topup_dari_menu") else _kb_topup_bayar()
    await update.message.reply_text(ft(teks), reply_markup=kb)
    return TOPUP_BAYAR


async def topup_bayar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if q.data == "topup:batal":
        await q.edit_message_text(ft("Topup dibatalkan."))
        return ConversationHandler.END
    amount = context.user_data.pop("topup_amount", 0)
    if not amount:
        await q.edit_message_text(ft("Nominal topup tidak ketemu, ulangi /topup ya."))
        return ConversationHandler.END
    tid = billing.buat_topup(uid, amount)
    kb_admin = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2705 Setujui", callback_data="topup:ok:%s" % tid),
         InlineKeyboardButton("\u274c Tolak", callback_data="topup:no:%s" % tid)],
    ])
    teks_admin = ("Topup baru!\nID: %s\nUser: %s (%d)\nNominal: Rp %s"
                  % (tid, q.from_user.full_name, uid, billing.rp(amount)))
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(int(aid), ft(teks_admin), reply_markup=kb_admin)
        except Exception:
            pass
    await q.edit_message_text(ft(
        "Sip! Topup Rp %s (ID %s) udah gw terusin ke admin. "
        "Tunggu dikonfirmasi ya, saldo masuk otomatis kalau disetujui."
        % (billing.rp(amount), tid)))
    return ConversationHandler.END


async def topup_admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("Khusus admin.", show_alert=True)
        return
    _, aksi, tid = q.data.split(":")
    tp = billing.get_topup(tid)
    if not tp:
        await q.edit_message_text(ft("Topup %s tidak ditemukan." % tid))
        return
    tuid = int(tp["uid"])
    amount = int(tp["amount"])
    if aksi == "ok":
        ok, hasil = billing.approve_topup(tid)
        if not ok:
            await q.answer(hasil, show_alert=True)
            return
        try:
            await context.bot.send_message(tuid, ft(
                "Topup Rp %s disetujui admin! Saldo kamu sekarang Rp %s. Gas bikin video!"
                % (billing.rp(amount), billing.rp(hasil))))
        except Exception:
            pass
        await q.edit_message_text(ft("Topup %s (Rp %s) DISETUJUI. Saldo user bertambah."
                                    % (tid, billing.rp(amount))))
    else:
        ok, hasil = billing.reject_topup(tid)
        if not ok:
            await q.answer(hasil, show_alert=True)
            return
        try:
            await context.bot.send_message(tuid, ft(
                "Topup Rp %s ditolak admin. Hubungi admin kalau kamu merasa sudah bayar ya."
                % billing.rp(amount)))
        except Exception:
            pass
        await q.edit_message_text(ft("Topup %s (Rp %s) DITOLAK." % (tid, billing.rp(amount))))


async def setharga_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text(ft(
            "Harga sekarang: Reguler Rp %s, Prioritas Rp %s per video.\n"
            "Pakai: /setharga <reguler> <prioritas>"
            % (billing.rp(billing.harga_reguler()),
               billing.rp(billing.harga_prioritas()))))
        return
    try:
        reg, prio = billing.set_harga_tier(int(context.args[0]),
                                           int(context.args[1]))
    except (ValueError, TypeError):
        await update.message.reply_text(ft("Nominal tidak valid."))
        return
    await update.message.reply_text(ft(
        "Harga diubah: Reguler Rp %s, Prioritas Rp %s."
        % (billing.rp(reg), billing.rp(prio))))


async def setref_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text(ft(
            "Bonus referal sekarang: pengguna baru Rp %s, pengundang Rp %s.\n"
            "Pakai: /setref <baru> <pengundang>"
            % (billing.rp(billing.bonus_baru_efektif()),
               billing.rp(billing.bonus_pengundang_efektif()))))
        return
    try:
        baru, pengundang = billing.set_bonus_referal(int(context.args[0]),
                                                     int(context.args[1]))
    except (ValueError, TypeError):
        await update.message.reply_text(ft("Nominal tidak valid."))
        return
    await update.message.reply_text(ft(
        "Bonus referal diubah: pengguna baru Rp %s, pengundang Rp %s."
        % (billing.rp(baru), billing.rp(pengundang))))


async def referal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _tolak_jika_komersial_nonaktif(update, context):
        return
    uid = update.effective_user.id
    try:
        me = await context.bot.get_me()
        username = me.username or ""
    except Exception:
        username = ""
    link = billing.get_link_referal(username, uid)
    jml, bonus = billing.info_referal(uid)
    pos = antrean.posisi_antrean(uid)
    teks = ("Link referal kamu:\n%s\n\n"
            "Teman diundang: %d\n"
            "Total bonus didapat: Rp %s." % (link, jml, billing.rp(bonus)))
    if pos:
        teks += "\n\nPosisi antreanmu: #%d." % pos
    await _balas_menu(update, teks, reply_markup=kb_kembali_utama())

async def setgoogle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args or []
    if len(args) < 2:
        key, cx = billing.get_google_api_key(), billing.get_google_cx()
        status = "terisi" if (key and cx) else "kosong (fallback TinyFish aktif)"
        await update.message.reply_text(ft("Google Custom Search: %s\n\nPakai: /setgoogle <api_key> <cx>" % status))
        return
    billing.set_google_api_key(args[0])
    billing.set_google_cx(args[1])
    await update.message.reply_text(ft("Google Custom Search diaktifkan."))


async def setpayment_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    teks = " ".join(context.args).strip()
    if not teks:
        await update.message.reply_text(ft(
            "Info pembayaran sekarang:\n\n%s\n\nPakai: /setpayment <teks baru>"
            % billing.get_payment_info()))
        return
    billing.set_payment_info(teks)
    await update.message.reply_text(ft("Info pembayaran diperbarui."))


async def voucher_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    a = context.args
    if len(a) < 3:
        await update.message.reply_text(ft(
            "Pakai: /voucher_add <kode> <discount|credit|unlimited> <nilai> [max_uses] [hari_berlaku]"))
        return
    try:
        kode = billing.voucher_add(a[0], a[1], a[2],
                                   a[3] if len(a) > 3 else 1,
                                   a[4] if len(a) > 4 else 30)
    except (ValueError, TypeError) as e:
        await update.message.reply_text(ft("Gagal: %s" % e))
        return
    await update.message.reply_text(ft("Voucher %s dibuat (tipe %s, nilai %s)." % (kode, a[1], a[2])))


async def voucher_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    v = billing.voucher_list()
    if not v:
        await _balas_menu(update, "Belum ada voucher.",
                              reply_markup=kb_kembali_utama())
        return
    lines = []
    for kode, x in sorted(v.items()):
        sisa = int(x.get("max_uses", 0)) - int(x.get("used_count", 0))
        st = "aktif" if x.get("active") else "nonaktif"
        lines.append("%s: %s %s, sisa %d/%d, %s"
                     % (kode, x.get("type"), x.get("value"), sisa, x.get("max_uses"), st))
    await _balas_menu(update, "Daftar voucher:\n" + "\n".join(lines),
                          reply_markup=kb_kembali_utama())


async def voucher_del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text(ft("Pakai: /voucher_del <kode>"))
        return
    if billing.voucher_del(context.args[0]):
        await update.message.reply_text(ft("Voucher %s dinonaktifkan." % context.args[0].upper()))
    else:
        await update.message.reply_text(ft("Voucher tidak ditemukan."))


async def topup_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    p = billing.topup_pending()
    if not p:
        await _balas_menu(update, "Tidak ada topup pending.",
                              reply_markup=kb_kembali_utama())
        return
    lines = []
    for tid, x in sorted(p.items(), key=lambda kv: kv[1].get("created_at", 0)):
        lines.append("%s: user %s, Rp %s" % (tid, x.get("uid"), billing.rp(x.get("amount", 0))))
    await _balas_menu(update, "Topup pending:\n" + "\n".join(lines),
                          reply_markup=kb_kembali_utama())


topup_conv = ConversationHandler(
    entry_points=[CommandHandler("topup", topup_mulai)],
    states={
        TOPUP_NOMINAL: [CallbackQueryHandler(topup_nominal, pattern=r"^topup:(nom:.+|batal)$")],
        TOPUP_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, topup_custom)],
        TOPUP_BAYAR: [CallbackQueryHandler(topup_bayar, pattern=r"^topup:(bayar|batal)$")],
    },
    fallbacks=[CommandHandler("cancel", cancel),
               CallbackQueryHandler(topup_bayar, pattern=r"^topup:batal$")],
)


B_KEMBALI = chr(0x2B05) + chr(0xFE0F) + " Kembali"


def kb_kembali_utama():
    # Satu tombol Kembali ke menu utama.
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(B_KEMBALI, callback_data="menu:utama")]])


async def _edit_q(q, teks, reply_markup=None, parse_mode=None):
    # Edit pesan callback; abaikan error bila isi tidak berubah.
    try:
        await q.edit_message_text(ft(teks), reply_markup=reply_markup,
                                  parse_mode=parse_mode)
    except Exception:
        pass


async def _balas_menu(update, teks, reply_markup=None, parse_mode=None):
    # Tampilkan teks: edit pesan yang ada bila dipicu tombol,
    # kirim pesan baru bila dipicu command.
    q = update.callback_query
    if q is not None:
        try:
            await q.answer()
        except Exception:
            pass
        await _edit_q(q, teks, reply_markup=reply_markup, parse_mode=parse_mode)
        return q.message
    return await update.message.reply_text(ft(teks), reply_markup=reply_markup,
                                           parse_mode=parse_mode)


def _teks_komersial_nonaktif():
    return "Fitur komersial nonaktif sementara."


async def _tolak_jika_komersial_nonaktif(update, context):
    # Tampilkan pesan nonaktif + tombol Kembali. True = akses diblokir.
    if billing.komersial_aktif():
        return False
    await _balas_menu(update, _teks_komersial_nonaktif(), reply_markup=kb_kembali_utama())
    return True


async def menu_utama_cb(update, context):
    # Tombol menu:utama -> tampilkan menu utama (/start), edit pesan.
    q = update.callback_query
    await q.answer()
    if await tolak_kalau_ban(update, context):
        return
    return await _ke_menu(q, get_session(q.from_user.id))


async def menu_back_cb(update, context):
    # Tombol menu:back -> kembali kontekstual (default: menu utama).
    q = update.callback_query
    await q.answer()
    if await tolak_kalau_ban(update, context):
        return
    tujuan = context.user_data.get("menu_kembali_ke", "menu:utama")
    if tujuan == "menu:topup":
        return await _topup_tampil_nominal_menu(update, context)
    return await _ke_menu(q, get_session(q.from_user.id))


def _kb_topup_nominal_menu():
    rows = [[InlineKeyboardButton("Rp %s" % billing.rp(n),
                                  callback_data="topup:nom:%d" % n)]
            for n in TOPUP_NOMINALS]
    rows.append([InlineKeyboardButton("Nominal lain",
                                      callback_data="topup:nom:custom")])
    rows.append([InlineKeyboardButton(B_KEMBALI, callback_data="menu:utama")])
    return InlineKeyboardMarkup(rows)


def _kb_topup_bayar_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2705 Sudah Bayar",
                              callback_data="topup:bayar")],
        [InlineKeyboardButton(B_KEMBALI, callback_data="menu:back")],
    ])


async def _topup_tampil_nominal_menu(update, context):
    if await _tolak_jika_komersial_nonaktif(update, context):
        return
    # Pilihan nominal topup dipicu tombol menu (edit pesan, bukan baru).
    q = update.callback_query
    uid = q.from_user.id
    if is_admin(uid):
        await _edit_q(q, "Kamu admin, gratis terus. Tidak perlu topup.",
                      reply_markup=kb_kembali_utama())
        return MENU
    context.user_data.pop("topup_amount", None)
    context.user_data["topup_dari_menu"] = True
    await _edit_q(q, "Mau topup berapa? Pilih nominal:",
                  reply_markup=_kb_topup_nominal_menu())
    return TOPUP_NOMINAL


async def redeem_kode(update, context):
    if await _tolak_jika_komersial_nonaktif(update, context):
        return
    # Terima kode voucher yang diketik user (dipicu tombol Redeem).
    kode = (update.message.text or "").strip()
    ok, pesan = billing.redeem_voucher(update.effective_user.id, kode)
    await update.message.reply_text(ft(pesan),
                                    reply_markup=kb_kembali_utama())
    return MENU



async def on_error(update: object, context) -> None:
    # Global error handler: abaikan error Telegram yang jinak
    # (edit pesan tanpa perubahan / callback query kedaluwarsa),
    # catat error lain agar tidak hilang diam-diam.
    err = context.error
    msg = str(err)
    jinak = (
        "Message is not modified" in msg
        or "message is not modified" in msg
        or "Query is too old" in msg
        or "query id is invalid" in msg
    )
    if jinak:
        log.info("Abaikan error jinak Telegram: %s", msg)
        return
    log.error("Unhandled exception: %r", err)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set SUTRADARA_BOT_TOKEN dulu")
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start),
                      CallbackQueryHandler(edit_mulai, pattern=r"^gas:edit:(style|audio|frame|durasi)$")],
        states={
            MENU: [CallbackQueryHandler(menu_utama_cb, pattern=r"^menu:utama$"),
                   CallbackQueryHandler(menu_back_cb, pattern=r"^menu:back$"),
                   CallbackQueryHandler(menu_picked, pattern=r"^menu:"),
               CallbackQueryHandler(set_cb, pattern=r"^set:"),
                   CallbackQueryHandler(back_to_menu, pattern=r"^nav:back$")],
            TOPUP_NOMINAL: [
                CallbackQueryHandler(menu_utama_cb, pattern=r"^menu:utama$"),
                CallbackQueryHandler(menu_back_cb, pattern=r"^menu:back$"),
                CallbackQueryHandler(topup_nominal, pattern=r"^topup:(nom:.+|batal)$")],
            TOPUP_CUSTOM: [
               MessageHandler(filters.TEXT & ~filters.COMMAND, set_terima),
                MessageHandler(filters.TEXT & ~filters.COMMAND, topup_custom),
                CallbackQueryHandler(menu_utama_cb, pattern=r"^menu:utama$")],
            TOPUP_BAYAR: [
                CallbackQueryHandler(menu_utama_cb, pattern=r"^menu:utama$"),
                CallbackQueryHandler(menu_back_cb, pattern=r"^menu:back$"),
                CallbackQueryHandler(topup_bayar, pattern=r"^topup:(bayar|batal)$")],
            REDEEM_KODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, redeem_kode),
                CallbackQueryHandler(menu_utama_cb, pattern=r"^menu:utama$")],
            RISET_KAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, riset_kategori),
                        CallbackQueryHandler(back_to_menu, pattern=r"^nav:back$")],
            PILIH_PRODUK: [CallbackQueryHandler(produk_picked, pattern=r"^prod:"),
                           CallbackQueryHandler(back_dari_produk, pattern=r"^nav:back$")],
            MANUAL_NAMA: [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_nama),
                        CallbackQueryHandler(back_to_menu, pattern=r"^nav:back$")],
            MANUAL_DESK: [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_deskripsi),
                        CallbackQueryHandler(back_dari_desk, pattern=r"^nav:back$")],
            OPSI_STYLE: [CallbackQueryHandler(style_picked, pattern=r"^style:"),
                         CallbackQueryHandler(back_dari_style, pattern=r"^nav:back$")] + _AUTO_HANDLERS,
            OPSI_AUDIO: [CallbackQueryHandler(audio_picked, pattern=r"^audio:"),
                         CallbackQueryHandler(back_dari_audio, pattern=r"^nav:back$")] + _AUTO_HANDLERS,
            OPSI_FRAME: [CallbackQueryHandler(frame_picked, pattern=r"^frame:"),
                         CallbackQueryHandler(back_dari_frame, pattern=r"^nav:back$")] + _AUTO_HANDLERS,
            OPSI_DURASI: [CallbackQueryHandler(durasi_picked, pattern=r"^dur:"),
                          CallbackQueryHandler(back_dari_durasi, pattern=r"^nav:back$")],
            FOTO: [
                MessageHandler(filters.PHOTO, foto_diterima),
                CallbackQueryHandler(foto_ai, pattern=r"^foto:ai$"),
                CallbackQueryHandler(foto_ai_ok, pattern=r"^foto:ai_ok$"),
                CallbackQueryHandler(foto_kembali_opsi,
                                      pattern=r"^foto:ai_back$"),
                CallbackQueryHandler(foto_web, pattern=r"^foto:web$"),
                CallbackQueryHandler(foto_keep, pattern=r"^foto:keep$"),
                CallbackQueryHandler(webcat_use, pattern=r"^webcat:use$"),
                CallbackQueryHandler(webcat_next, pattern=r"^webcat:next$"),
                CallbackQueryHandler(webcat_batal,
                                      pattern=r"^webcat:(cancel|back)$"),
        CallbackQueryHandler(webcat_upload, pattern=r"^webcat:upload$"),
                CallbackQueryHandler(back_dari_foto, pattern=r"^nav:back$"),
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )
    app.add_handler(conv)
    appeal_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(appeal_mulai,
                                           pattern=r"^appeal:ajukan$")],
        states={APPEAL_TULIS: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, appeal_tulis),
            CallbackQueryHandler(appeal_batal, pattern=r"^appeal:batal$")]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    bc_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(bc_mulai, pattern=r"^admin:bc$")],
        states={
            BC_TULIS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bc_tulis),
                CallbackQueryHandler(bc_batal, pattern=r"^admin:bc_no$")],
            BC_KONFIRM: [
                CallbackQueryHandler(bc_kirim, pattern=r"^admin:bc_go$"),
                CallbackQueryHandler(bc_batal, pattern=r"^admin:bc_no$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(appeal_conv)
    app.add_handler(bc_conv)
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(
        appeal_putusan, pattern=r"^appeal:(terima|tolak)"))
    app.add_handler(CallbackQueryHandler(gas_stop, pattern=r"^gas:stop$"))
    app.add_handler(CallbackQueryHandler(gas_video, pattern=r"^gas:"))
    app.add_handler(CallbackQueryHandler(varian_picked, pattern=r"^varian:"))
    app.add_handler(topup_conv)
    app.add_handler(CommandHandler("saldo", saldo_cmd))
    app.add_handler(CommandHandler("redeem", redeem_cmd))
    app.add_handler(CommandHandler("setharga", setharga_cmd))
    app.add_handler(CommandHandler("setpayment", setpayment_cmd))
    app.add_handler(CommandHandler("setgoogle", setgoogle_cmd))
    app.add_handler(CommandHandler("voucher_add", voucher_add_cmd))
    app.add_handler(CommandHandler("voucher_list", voucher_list_cmd))
    app.add_handler(CommandHandler("voucher_del", voucher_del_cmd))
    app.add_handler(CommandHandler("topup_list", topup_list_cmd))
    app.add_handler(CallbackQueryHandler(topup_admin_cb, pattern=r"^topup:(ok|no):"))
    app.add_handler(CommandHandler("referal", referal_cmd))
    app.add_handler(CommandHandler("setref", setref_cmd))
    app.add_handler(CommandHandler("dasbor", dasbor_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("setbatas", setbatas_cmd))
    app.add_handler(CommandHandler("setlimit", setlimit_cmd))
    app.add_handler(CommandHandler("setjeda", setjeda_cmd))
    app.add_handler(CommandHandler("setantri", setantri_cmd))
    app.add_handler(CommandHandler("setmintopup", setmintopup_cmd))
    app.add_handler(CommandHandler("setestimasi", setestimasi_cmd))
    app.add_handler(CommandHandler("setelan", setelan_cmd))
    app.add_handler(CommandHandler("komersial", komersial_cmd))
    log.info("Sutradara Bot v3 jalan...")
    app.post_init = _start_worker
    app.run_polling()


async def _gas_masuk_antrean(context, chat_id, s, uid, tier, gratis=False):
    # Cek apakah user memiliki proses aktif
    aktif = proses_user_aktif(uid)
    if aktif:
        _, pesan = aktif
        await say(context, chat_id, s or {}, pesan, fresh=True)
        return

    # MASTER SWITCH: non-komersial -> skip tier/saldo/antrean, langsung generate (1 sesi job_lock).
    if not billing.komersial_aktif():
        if s.get("is_generating_video") or job_lock.locked():
            await say(context, chat_id, s or {},
                      "⚠️ *Masih ada proses generate video yang sedang berjalan!* Mohon tunggu sampai videomu selesai diproses ya, jangan membuat proses baru.",
                      fresh=True)
            return

        msg_nonaktif = ("Mode komersial nonaktif: generate langsung tanpa pilih "
                        "tier, tanpa potong saldo, dan tanpa antrean. Tetap 1 sesi "
                        "dalam satu waktu demi keamanan (anti-ban).")
        await say(context, chat_id, s, msg_nonaktif, fresh=True)
        # FIX routing: Gas Bikin Video -> alur generate video
        # (_proses_antrean), bukan mengulang produksi_awal (enhance katalog).
        job_data = {
            "chat_id": chat_id,
            "tier": "reguler",
            "bill_harga": 0,
            "workdir": str(s.get("workdir")),
            "produk": s.get("produk"),
            "deskripsi": s.get("deskripsi", ""),
            "style": s.get("style"),
            "audio": s.get("audio"),
            "framing": s.get("framing"),
            "durasi": s.get("durasi"),
            "frames": list(s.get("frames") or []),
            "board": s.get("board"),
            "antrean_msg_id": s.get("status_id"),
        }
        s["is_generating_video"] = True
        try:
            async with job_lock:
                await _proses_antrean(
                    context.application, {"uid": uid, "job_data": job_data})
        finally:
            s["is_generating_video"] = False
        return
    tier = "prioritas" if tier == "prioritas" else "reguler"
    pos_lama = antrean.posisi_antrean(uid)
    if pos_lama:
        await say(context, chat_id, s,
                  "⏳ Kamu sudah di antrean. Posisi *#%d* (estimasi ~%d menit). Tunggu sampai giliranmu ya!"
                  % (pos_lama, antrean.estimasi_waktu()),
                  parse_mode="Markdown", fresh=True)
        return
    # FASE 4: batasi generate harian untuk user unlimited
    if billing.is_unlimited(uid) and not is_admin(uid):
        boleh, pesan_batas = billing.cek_unlimited_harian(uid)
        if not boleh:
            await say(context, chat_id, s, pesan_batas, fresh=True)
            return
    bill_harga = 0
    if not gratis:
        bill_harga = billing.harga_video_segmen(uid, tier, max(1, int(s.get("durasi", 8)) // 8))
        if billing.get_saldo(uid) < bill_harga:
            await say(context, chat_id, s,
                      "Saldo tidak cukup (butuh Rp %s, saldo Rp %s). /topup untuk isi saldo."
                      % (billing.rp(bill_harga), billing.rp(billing.get_saldo(uid))),
                      fresh=True)
            return

    pesan_lama = []
    if s.get("status_id"):
        pesan_lama.append(s["status_id"])

    job_data = {
        "chat_id": chat_id,
        "tier": tier,
        "bill_harga": bill_harga,
        "workdir": str(s.get("workdir")),
        "produk": s.get("produk"),
        "deskripsi": s.get("deskripsi", ""),
        "style": s.get("style"),
        "audio": s.get("audio"),
        "framing": s.get("framing"),
        "durasi": s.get("durasi"),
        "frames": list(s.get("frames") or []),
        "board": s.get("board"),
        "pesan_lama_ids": pesan_lama,
    }
    # PANEL SETTINGS ADMIN: cek limit harian, jeda, max antrean (non-admin)
    if not is_admin(uid):
        if antrean.panjang_antrean() >= billing.max_antrean():
            await say(context, chat_id, s, "Antrian penuh, coba lagi nanti.", fresh=True)
            return
        boleh_limit, pesan_limit = billing.cek_limit_harian(uid)
        if not boleh_limit:
            await say(context, chat_id, s, pesan_limit, fresh=True)
            return
        boleh_jeda, pesan_jeda = billing.cek_jeda(uid)
        if not boleh_jeda:
            await say(context, chat_id, s, pesan_jeda, fresh=True)
            return
    pos = antrean.tambah_antrean(uid, tier, job_data)
    if not is_admin(uid):
        billing.catat_generate(uid)
    if billing.is_unlimited(uid) and not is_admin(uid):
        billing.catat_unlimited_harian(uid)  # FASE 4: kuota harian unlimited
    est = antrean.estimasi_waktu()
    kb_batal = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Batalkan Antrian",
                              callback_data="gas:batal_antri")]])
    m_antri = await say(context, chat_id, s,
              "⏳ *Masuk antrean!*\n\n"
              "Posisi kamu: *#%d*\n"
              "Estimasi tunggu: *~%d menit*\n\n"
              "Santai dulu ya, nanti ada notifikasi otomatis kalau antreanmu terpenuhi! 🔔"
              % (pos, est),
              reply_markup=kb_batal, parse_mode="Markdown", fresh=True)
    if m_antri and hasattr(m_antri, "message_id"):
        s["antrean_msg_id"] = m_antri.message_id
        antrean.update_job_data(uid, {"antrean_msg_id": m_antri.message_id})


async def komersial_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin: /komersial on|off - master switch fitur komersial.
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(ft("Khusus admin."))
        return
    arg = context.args[0].lower() if context.args else ""
    if arg in ("on", "aktif", "1", "true", "ya"):
        billing.set_komersial(True)
        await update.message.reply_text(ft("Mode komersial: AKTIF. Saldo, voucher, topup, referal, tier, dan antrean kembali normal. Semua data tersimpan tetap utuh (tidak direset)."))
        return
    if arg in ("off", "nonaktif", "0", "false", "tidak"):
        billing.set_komersial(False)
        await update.message.reply_text(ft("Mode komersial: NONAKTIF. Generate langsung tanpa pilih tier, tanpa potong saldo, dan tanpa antrean (tetap 1 sesi dalam satu waktu demi keamanan). Tombol Saldo/Topup/Redeem/Referal menampilkan pesan nonaktif. Semua data tersimpan tetap utuh (tidak direset)."))
        return
    status = "AKTIF" if billing.komersial_aktif() else "NONAKTIF"
    await update.message.reply_text(ft("Mode komersial saat ini: " + status + ". Pakai: /komersial on | /komersial off"))


async def dasbor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    st = billing.get_statistik()
    kredit = await asyncio.to_thread(billing.flow2api_kredit)
    if kredit:
        baris = ["\u26a1 FLOW2API: %s kredit (%d akun)" % (billing.rp(kredit["total"]), kredit["n_akun"])]
        for email_m, c in kredit["per_akun"]:
            baris.append("   \u2022 %s: %s" % (email_m, billing.rp(c)))
        teks_kredit = "\n".join(baris)
    else:
        teks_kredit = "\u26a1 FLOW2API: tidak tersedia"
    kapan = time.strftime("%d-%m-%Y %H:%M", time.localtime())
    teks = (
        "\U0001F4CA DASBOR BISNIS\n"
        "Mode komersial: %s\n"
        "\U0001F550 %s\n\n"
        "%s\n\n"
        "\U0001F465 USER: %d total\n"
        "\U0001F3AC VIDEO: %d generate\n"
        "\U0001F4B0 REVENUE: Rp %s (topup Rp %s + estimasi generate Rp %s)\n"
        "\U0001F4B3 SALDO BEREDAR: Rp %s\n"
        "\u23f3 ANTRIAN: %d (%d prioritas)\n"
        "\U0001F39e\ufe0f VOUCHER: %d kode aktif, %d redeem\n"
        "\U0001F91D REFERAL: %d sukses, bonus Rp %s dibagikan"
    ) % (
        "AKTIF" if billing.komersial_aktif() else "NONAKTIF",
        kapan,
        teks_kredit,
        st["total_user"],
        st["total_generate"],
        billing.rp(st["revenue_total"]),
        billing.rp(st["revenue_topup"]),
        billing.rp(st["estimasi_generate"]),
        billing.rp(st["total_saldo"]),
        st["antri"],
        st["antri_prioritas"],
        st["voucher_aktif"],
        st["voucher_redeem"],
        st["ref_sukses"],
        billing.rp(st["ref_bonus"]),
    )
    await _balas_menu(update, teks, reply_markup=kb_kembali_utama())


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # FASE 4: /menu adalah alias untuk /start (tampilkan menu tombol).
    return await start(update, context)

async def setbatas_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        teks = chr(10).join(["Batas harian unlimited sekarang: %d video/hari." % billing.batas_unlimited_harian(),
                             "Pakai: /setbatas <angka>"])
        await update.message.reply_text(ft(teks))
        return
    try:
        batas = billing.set_batas_unlimited(int(context.args[0]))
    except (ValueError, TypeError):
        await update.message.reply_text(ft("Angka tidak valid (minimal 1)."))
        return
    await update.message.reply_text(ft("Batas harian unlimited diubah: %d video/hari." % batas))


async def setlimit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        teks = chr(10).join(["Limit video harian per user sekarang: %d/hari (0 = tanpa batas)." % billing.limit_harian(),
                             "Pakai: /setlimit <angka>"])
        await update.message.reply_text(ft(teks))
        return
    try:
        batas = billing.set_limit_harian(int(context.args[0]))
    except (ValueError, TypeError):
        await update.message.reply_text(ft("Angka tidak valid (minimal 0, 0 = tanpa batas)."))
        return
    await update.message.reply_text(ft("Limit harian diubah: %d video/hari (0 = tanpa batas)." % batas))

async def setjeda_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        teks = chr(10).join(["Jeda antar generate sekarang: %d detik." % billing.jeda_generate(),
                             "Pakai: /setjeda <detik>"])
        await update.message.reply_text(ft(teks))
        return
    try:
        jeda = billing.set_jeda_generate(int(context.args[0]))
    except (ValueError, TypeError):
        await update.message.reply_text(ft("Angka tidak valid (minimal 0)."))
        return
    await update.message.reply_text(ft("Jeda antar generate diubah: %d detik." % jeda))

async def setantri_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        teks = chr(10).join(["Max panjang antrean sekarang: %d." % billing.max_antrean(),
                             "Pakai: /setantri <angka>"])
        await update.message.reply_text(ft(teks))
        return
    try:
        maks = billing.set_max_antrean(int(context.args[0]))
    except (ValueError, TypeError):
        await update.message.reply_text(ft("Angka tidak valid (minimal 1)."))
        return
    await update.message.reply_text(ft("Max antrean diubah: %d." % maks))

async def setmintopup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        teks = chr(10).join(["Minimal topup sekarang: Rp %s." % billing.rp(billing.min_topup()),
                             "Pakai: /setmintopup <rupiah>"])
        await update.message.reply_text(ft(teks))
        return
    try:
        mini = billing.set_min_topup(int(context.args[0]))
    except (ValueError, TypeError):
        await update.message.reply_text(ft("Nominal tidak valid."))
        return
    await update.message.reply_text(ft("Minimal topup diubah: Rp %s." % billing.rp(mini)))

async def setestimasi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        teks = chr(10).join(["Estimasi waktu per video sekarang: %d menit." % billing.estimasi_menit(),
                             "Pakai: /setestimasi <menit>"])
        await update.message.reply_text(ft(teks))
        return
    try:
        est = billing.set_estimasi_menit(int(context.args[0]))
    except (ValueError, TypeError):
        await update.message.reply_text(ft("Angka tidak valid (minimal 1)."))
        return
    await update.message.reply_text(ft("Estimasi per video diubah: %d menit." % est))

def _teks_setelan():
    pay = (billing.get_payment_info() or "").strip().split(chr(10))
    pay_singkat = pay[0] if pay and pay[0] else "-"
    if len(pay_singkat) > 60:
        pay_singkat = pay_singkat[:60] + "..."
    limit = billing.limit_harian()
    limit_teks = "tanpa batas" if limit <= 0 else "%d/hari" % limit
    return chr(10).join([
        "Setelan bot saat ini:",
        "Mode komersial: %s" % ("AKTIF" if billing.komersial_aktif() else "NONAKTIF"),
        "Harga reguler: Rp %s" % billing.rp(billing.harga_reguler()),
        "Harga prioritas: Rp %s" % billing.rp(billing.harga_prioritas()),
        "Bonus referal: baru Rp %s, pengundang Rp %s"
        % (billing.rp(billing.bonus_baru_efektif()), billing.rp(billing.bonus_pengundang_efektif())),
        "Batas harian unlimited: %d/hari" % billing.batas_unlimited_harian(),
        "Limit harian user: %s" % limit_teks,
        "Jeda antar generate: %d detik" % billing.jeda_generate(),
        "Max antrean: %d" % billing.max_antrean(),
        "Minimal topup: Rp %s" % billing.rp(billing.min_topup()),
        "Estimasi per video: %d menit" % billing.estimasi_menit(),
        "Info bayar: %s" % pay_singkat,
        "",
        "Pilih tombol di bawah untuk mengubah nilai.",
    ])

async def setelan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    sessions.setdefault(update.effective_user.id, {})["set_pending"] = None
    await update.message.reply_text(ft(_teks_setelan()), reply_markup=kb_settings())


# ===== PENGATURAN VIA TOMBOL (khusus admin) =====
SET_BUTTONS = [
    ("harga", "Harga"),
    ("bonus", "Bonus referal"),
    ("batas", "Batas unlimited/harian"),
    ("limit", "Limit video per hari"),
    ("jeda", "Jeda antar generate"),
    ("antri", "Max antrean"),
    ("mintopup", "Min topup"),
    ("estimasi", "Estimasi per video"),
    ("payment", "Info pembayaran"),
]


def kb_settings():
    rows = [[InlineKeyboardButton(label, callback_data="set:" + key)]
            for key, label in SET_BUTTONS]
    rows.append([InlineKeyboardButton("Kembali ke menu utama",
                                      callback_data="menu:utama")])
    return InlineKeyboardMarkup(rows)


def _set_prompt(key):
    if key == "harga":
        return ("Harga\n\nSaat ini:\nReguler: Rp %s\nPrioritas: Rp %s"
                "\n\nKetik nilai baru dengan format:\nREGULER PRIORITAS"
                "\nContoh: 5000 15000"
                % (billing.rp(billing.harga_reguler()),
                   billing.rp(billing.harga_prioritas())))
    if key == "bonus":
        return ("Bonus referal\n\nSaat ini:\nUser baru: Rp %s\nPengundang: Rp %s"
                "\n\nKetik nilai baru dengan format:\nBARU PENGUNDANG"
                "\nContoh: 2000 5000"
                % (billing.rp(billing.bonus_baru_efektif()),
                   billing.rp(billing.bonus_pengundang_efektif())))
    if key == "batas":
        return ("Batas unlimited/harian\n\nSaat ini: %d/hari"
                "\n\nKetik angka baru (jumlah video per hari)."
                % billing.batas_unlimited_harian())
    if key == "limit":
        lim = billing.limit_harian()
        return ("Limit video per hari\n\nSaat ini: %s"
                "\n\nKetik angka baru (0 = tanpa batas)."
                % ("tanpa batas" if lim <= 0 else "%d/hari" % lim))
    if key == "jeda":
        return ("Jeda antar generate\n\nSaat ini: %d detik"
                "\n\nKetik angka baru (detik)."
                % billing.jeda_generate())
    if key == "antri":
        return ("Max antrean\n\nSaat ini: %d\n\nKetik angka baru."
                % billing.max_antrean())
    if key == "mintopup":
        return ("Minimal topup\n\nSaat ini: Rp %s"
                "\n\nKetik angka baru (rupiah)."
                % billing.rp(billing.min_topup()))
    if key == "estimasi":
        return ("Estimasi per video\n\nSaat ini: %d menit"
                "\n\nKetik angka baru (menit)."
                % billing.estimasi_menit())
    if key == "payment":
        info = (billing.get_payment_info() or "").strip() or "-"
        return ("Info pembayaran\n\nSaat ini:\n%s"
                "\n\nKetik teks baru (boleh beberapa baris)."
                % info)
    return None


def _set_angka(teks):
    return int(teks.replace(".", "").replace(",", "").replace(" ", "").strip())


async def _set_tampilkan(q, catatan=None):
    teks = _teks_setelan()
    if catatan:
        teks = catatan + "\n\n" + teks
    await _edit_q(q, teks, reply_markup=kb_settings())


async def _set_edit_status(context, chat_id, uid, teks, reply_markup=None):
    s = sessions.get(uid) or {}
    mid = s.get("status_id")
    if not mid:
        return
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=mid,
                                            text=ft(teks), reply_markup=reply_markup)
    except Exception:
        pass


async def set_cb(update, context):
    q = update.callback_query
    await q.answer()
    if await tolak_kalau_ban(q, context):
        return
    if not is_admin(q.from_user.id):
        await q.answer("Khusus admin.", show_alert=True)
        return MENU
    s = sessions.setdefault(q.from_user.id, {})
    key = q.data.split(":", 1)[1]
    if key == "menu":
        s["set_pending"] = None
        await _set_tampilkan(q)
        return MENU
    prompt = _set_prompt(key)
    if prompt is None:
        s["set_pending"] = None
        await _set_tampilkan(q)
        return MENU
    s["set_pending"] = key
    s["set_pending_ts"] = time.time()
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Kembali", callback_data="set:menu")]])
    await _edit_q(q, prompt + "\n\nKetik nilai baru, atau /cancel untuk batal.",
                reply_markup=kb)
    return MENU


async def set_terima(update, context):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    s = sessions.setdefault(uid, {})
    key = s.get("set_pending")
    if not key:
        return None
    try:
        umur = time.time() - float(s.get("set_pending_ts") or 0)
    except (TypeError, ValueError):
        umur = 9999
    if umur > 300:
        s["set_pending"] = None
        return None
    teks = (update.message.text or "").strip()
    if key == "payment":
        billing.set_payment_info("" if teks == "-" else teks)
        catatan = "Info pembayaran diubah."
    else:
        parts = teks.split()
        try:
            if key == "harga":
                if len(parts) != 2:
                    raise ValueError("format")
                reg, prio = _set_angka(parts[0]), _set_angka(parts[1])
                if reg < 0 or prio < 0:
                    raise ValueError("negatif")
                reg, prio = billing.set_harga_tier(reg, prio)
                catatan = ("Harga diubah: reguler Rp %s, prioritas Rp %s."
                           % (billing.rp(reg), billing.rp(prio)))
            elif key == "bonus":
                if len(parts) != 2:
                    raise ValueError("format")
                baru, undang = _set_angka(parts[0]), _set_angka(parts[1])
                if baru < 0 or undang < 0:
                    raise ValueError("negatif")
                baru, undang = billing.set_bonus_referal(baru, undang)
                catatan = ("Bonus referal diubah: baru Rp %s, pengundang Rp %s."
                           % (billing.rp(baru), billing.rp(undang)))
            elif key == "batas":
                n = _set_angka(teks)
                if n < 0:
                    raise ValueError("negatif")
                billing.set_batas_unlimited(n)
                catatan = "Batas unlimited/harian diubah: %d/hari." % n
            elif key == "limit":
                n = _set_angka(teks)
                if n < 0:
                    raise ValueError("negatif")
                billing.set_limit_harian(n)
                catatan = ("Limit video per hari diubah: %s."
                           % ("tanpa batas" if n <= 0 else "%d/hari" % n))
            elif key == "jeda":
                n = _set_angka(teks)
                if n < 0:
                    raise ValueError("negatif")
                billing.set_jeda_generate(n)
                catatan = "Jeda antar generate diubah: %d detik." % n
            elif key == "antri":
                n = _set_angka(teks)
                if n < 0:
                    raise ValueError("negatif")
                billing.set_max_antrean(n)
                catatan = "Max antrean diubah: %d." % n
            elif key == "mintopup":
                n = _set_angka(teks)
                if n < 0:
                    raise ValueError("negatif")
                billing.set_min_topup(n)
                catatan = "Minimal topup diubah: Rp %s." % billing.rp(n)
            elif key == "estimasi":
                n = _set_angka(teks)
                if n < 0:
                    raise ValueError("negatif")
                billing.set_estimasi_menit(n)
                catatan = "Estimasi per video diubah: %d menit." % n
            else:
                s["set_pending"] = None
                return None
        except (ValueError, TypeError):
            prompt = _set_prompt(key) or ""
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Kembali", callback_data="set:menu")]])
            await _set_edit_status(context, update.effective_chat.id, uid,
                                   "Input tidak valid, coba lagi.\n\n" + prompt
                                   + "\n\nKetik nilai baru, atau /cancel untuk batal.",
                                   kb)
            return MENU
    s["set_pending"] = None
    await _set_edit_status(context, update.effective_chat.id, uid,
                           catatan + "\n\n" + _teks_setelan(), kb_settings())
    return MENU

# ============================ ADMIN ============================
DURASI_BAN = [("1 jam", 3600), ("1 hari", 86400), ("7 hari", 604800),
              ("30 hari", 2592000)]


def _teks_panel_admin() -> tuple:
    st = stats()
    maint = "\U0001F7E2 ON" if get_meta("maintenance", False) else "\U0001F534 OFF"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001F465 Daftar User", callback_data="admin:users"),
         InlineKeyboardButton("\U0001F4CA Statistik", callback_data="admin:stats")],
        [InlineKeyboardButton("\U0001F4E2 Broadcast", callback_data="admin:bc"),
         InlineKeyboardButton(f"\U0001F527 Maintenance: {maint}",
                              callback_data="admin:maint")],
    ])
    teks = (f"\U0001F6E1\ufe0f *Panel Admin*\n\n\U0001F465 User: {st['total_user']} | "
            f"\u26d4 Banned: {st['banned']}\n"
            f"\U0001F3AC Produksi hari ini: {st['prod_hari_ini']} "
            f"(total {st['prod_total']})")
    return teks, kb


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(ft("\u26d4 Khusus admin."))
        return
    teks, kb = _teks_panel_admin()
    await update.message.reply_text(ft(teks), parse_mode="Markdown",
                                    reply_markup=kb)


def _teks_user(uid: str) -> tuple:
    d = users_db.get(uid, {})
    uname = f"@{d['username']}" if d.get("username") else "(tanpa username)"
    bu = d.get("banned_until", 0)
    if bu == -1:
        status = "\u26d4 Ban permanen"
    elif bu and bu > time.time():
        status = ("\u26d4 Ban sampai "
                  + time.strftime("%d-%m-%Y %H:%M", time.localtime(bu)))
    else:
        status = "\u2705 Aktif"
    gabung = time.strftime("%d-%m-%Y", time.localtime(d.get("first_seen", 0))) \
        if d.get("first_seen") else "-"
    aktif = time.strftime("%d-%m-%Y %H:%M", time.localtime(d.get("last_seen", 0))) \
        if d.get("last_seen") else "-"
    teks = (f"\U0001F464 *{uname}* ({d.get('nama') or '-'})\n"
            f"\U0001F194 `{uid}`\n"
            f"\U0001F517 {d.get('link') or '-'}\n"
            f"\U0001F4C5 Gabung: {gabung} | Terakhir aktif: {aktif}\n"
            f"\U0001F3AC Produksi: {d.get('productions_total', 0)}x "
            f"(hari ini {d.get('prod_hari_ini', 0)}x)\n"
            f"\u26d4 Alasan ban: {d.get('ban_reason') or '-'}\n\n"
            f"Status: *{status}*")
    rows = [[InlineKeyboardButton(f"\u23f3 {label}",
                                  callback_data=f"admin:ban:{uid}:{detik}")]
            for label, detik in DURASI_BAN]
    rows.append([InlineKeyboardButton("\u26d4 Ban Permanen",
                                      callback_data=f"admin:ban:{uid}:perm")])
    rows.append([InlineKeyboardButton("\u2705 Unban",
                                      callback_data=f"admin:unban:{uid}"),
                 InlineKeyboardButton("\u2b05\ufe0f Kembali",
                                      callback_data="admin:users")])
    return teks, InlineKeyboardMarkup(rows)


async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    data = q.data
    if data == "admin:panel":
        teks, kb = _teks_panel_admin()
        await q.edit_message_text(ft(teks), parse_mode="Markdown",
                                  reply_markup=kb)
    elif data == "admin:users":
        users = daftar_user()
        if not users:
            await q.edit_message_text(
                ft("\U0001F4ED Belum ada user tercatat."),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("\u2b05\ufe0f Kembali",
                                         callback_data="admin:panel")]]))
            return
        btns = []
        for uid, d in users:
            label = f"@{(d.get('username') or uid)}"
            if d.get("banned_until") == -1 or d.get("banned_until", 0) > time.time():
                label = "\u26d4 " + label
            btns.append([InlineKeyboardButton(label[:40],
                                              callback_data=f"admin:user:{uid}")])
        btns.append([InlineKeyboardButton("\u2b05\ufe0f Kembali",
                                          callback_data="admin:panel")])
        await q.edit_message_text(ft("\U0001F465 *Daftar User* (tap buat detail):"),
                                  parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(btns))
    elif data.startswith("admin:user:"):
        uid = data.split(":")[2]
        teks, kb = _teks_user(uid)
        await q.edit_message_text(ft(teks), parse_mode="Markdown",
                                  reply_markup=kb)
    elif data.startswith("admin:ban:"):
        _, _, uid, dur = data.split(":")
        label = "permanen" if dur == "perm" else next(
            (l for l, s in DURASI_BAN if str(s) == dur), dur)
        ban_user(uid, None if dur == "perm" else int(dur), reason="by admin")
        try:
            await context.bot.send_message(
                int(uid),
                ft(f"\U0001F6AB Kamu di-ban {label} oleh admin.\n"
                   "Kalau merasa salah, ajukan banding \U0001F64F"),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("\U0001F4E9 Ajukan Banding",
                                         callback_data="appeal:ajukan")]]))
        except Exception:
            pass
        teks, kb = _teks_user(uid)
        await q.edit_message_text(ft(teks), parse_mode="Markdown",
                                  reply_markup=kb)
    elif data.startswith("admin:unban:"):
        uid = data.split(":")[2]
        unban_user(uid)
        try:
            await context.bot.send_message(
                int(uid),
                ft("\u2705 Ban kamu dicabut admin. Silakan /start lagi \U0001F389"))
        except Exception:
            pass
        teks, kb = _teks_user(uid)
        await q.edit_message_text(ft(teks), parse_mode="Markdown",
                                  reply_markup=kb)
    elif data == "admin:stats":
        st = stats()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "\u2b05\ufe0f Kembali", callback_data="admin:panel")]])
        await q.edit_message_text(
            ft(f"\U0001F4CA *Statistik*\n\n\U0001F465 Total user: {st['total_user']}\n"
               f"\u26d4 Sedang di-ban: {st['banned']}\n"
               f"\U0001F3AC Produksi hari ini: {st['prod_hari_ini']}\n"
               f"\U0001F3AC Total produksi: {st['prod_total']}"),
            parse_mode="Markdown", reply_markup=kb)
    elif data == "admin:maint":
        set_meta("maintenance", not get_meta("maintenance", False))
        teks, kb = _teks_panel_admin()
        await q.edit_message_text(ft(teks), parse_mode="Markdown",
                                  reply_markup=kb)


# ============================ BANDING ============================
async def appeal_mulai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    s = sessions.setdefault(uid, {"status_id": None})
    s["status_id"] = q.message.message_id
    s["mode_admin"] = "appeal"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "\u274c Batal", callback_data="appeal:batal")]])
    await q.edit_message_text(
        ft("\U0001F4E9 *Ajukan Banding*\n\nTulis alasan kenapa ban kamu harus dicabut:"),
        parse_mode="Markdown", reply_markup=kb)
    return APPEAL_TULIS


async def appeal_tulis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = sessions.get(uid, {})
    alasan = update.message.text.strip()
    d = users_db.get(str(uid), {})
    nama = f"@{d['username']}" if d.get("username") else (d.get("nama") or str(uid))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2705 Terima (Unban)",
                              callback_data=f"appeal:terima:{uid}")],
        [InlineKeyboardButton("\u274c Tolak",
                              callback_data=f"appeal:tolak:{uid}")],
    ])
    terkirim = 0
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(
                int(aid),
                ft(f"\U0001F4E9 *Banding Baru*\n\n\U0001F464 {nama} (`{uid}`)\n"
                   f"\U0001F517 {d.get('link') or '-'}\n\U0001F4DD Alasan:\n{alasan}"),
                parse_mode="Markdown", reply_markup=kb)
            terkirim += 1
        except Exception:
            pass
    s.pop("mode_admin", None)
    if terkirim:
        await update.message.reply_text(
            ft("\u2705 Banding terkirim ke admin. Tunggu kabar ya \U0001F64F"))
    else:
        await update.message.reply_text(
            ft("\u274c Gagal ngirim ke admin. Coba lagi nanti ya."))
    return ConversationHandler.END


async def appeal_batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sessions.get(q.from_user.id, {}).pop("mode_admin", None)
    await q.edit_message_text(ft("Dibatalkan."))
    return ConversationHandler.END


async def appeal_putusan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    _, putusan, uid = q.data.split(":")
    if putusan == "terima":
        unban_user(uid)
        hasil = "\u2705 DITERIMA"
        info = "Banding kamu *diterima* \u2014 ban dicabut, silakan /start lagi \U0001F389"
    else:
        hasil = "\u274c DITOLAK"
        info = "Banding kamu *ditolak* admin. Ban tetap berlaku."
    try:
        await context.bot.send_message(int(uid), ft(info),
                                       parse_mode="Markdown")
    except Exception:
        pass
    try:
        await q.edit_message_text(ft(f"{hasil} \u2014 banding dari `{uid}`"),
                                  parse_mode="Markdown")
    except Exception:
        pass


# ============================ BROADCAST ============================
async def bc_mulai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return None
    s = sessions.setdefault(q.from_user.id, {"status_id": None})
    s["status_id"] = q.message.message_id
    s["mode_admin"] = "bc"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "\u274c Batal", callback_data="admin:bc_no")]])
    await q.edit_message_text(
        ft("\U0001F4E2 *Broadcast*\n\nKetik pesan yang mau dikirim ke semua user:"),
        parse_mode="Markdown", reply_markup=kb)
    return BC_TULIS


async def bc_tulis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = sessions.get(update.effective_user.id, {})
    s["bc_draft"] = update.message.text
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001F4E2 Kirim ke Semua User",
                              callback_data="admin:bc_go")],
        [InlineKeyboardButton("\u274c Batal", callback_data="admin:bc_no")],
    ])
    try:
        await update.message.reply_text(
            ft(f"\U0001F4E2 *Preview Broadcast:*\n\n{update.message.text}"),
            parse_mode="Markdown", reply_markup=kb)
    except Exception:
        await update.message.reply_text(
            ft(f"\U0001F4E2 Preview Broadcast:\n\n{update.message.text}"),
            reply_markup=kb)
    return BC_KONFIRM
async def bc_kirim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return ConversationHandler.END
    s = sessions.get(q.from_user.id, {})
    draft = s.pop("bc_draft", "")
    s.pop("mode_admin", None)
    if not draft:
        await q.edit_message_text(ft("Draf kosong, dibatalkan."))
        return ConversationHandler.END
    await q.edit_message_text(ft("\U0001F4E2 Mengirim broadcast..."))
    ok = gagal = 0
    for uid, d in list(users_db.items()):
        if uid.startswith("_") or not isinstance(d, dict) or cek_ban(uid):
            continue
        try:
            await context.bot.send_message(int(uid), ft(draft),
                                           parse_mode="Markdown")
            ok += 1
        except Exception:
            try:
                await context.bot.send_message(int(uid), ft(draft))
                ok += 1
            except Exception:
                gagal += 1
    await q.edit_message_text(
        ft(f"\u2705 Broadcast terkirim: {ok} berhasil, {gagal} gagal."))
    return ConversationHandler.END


async def bc_batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = sessions.get(q.from_user.id, {})
    s.pop("bc_draft", None)
    s.pop("mode_admin", None)
    await q.edit_message_text(ft("Broadcast dibatalkan."))
    return ConversationHandler.END


async def tolak_kalau_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True kalau user sedang di-ban (pesan ban + tombol banding dikirim)."""
    d = cek_ban(update.effective_user.id)
    if not d:
        return False
    bu = d.get("banned_until", 0)
    kapan = "permanen" if bu == -1 else time.strftime("%d-%m-%Y %H:%M",
                                                      time.localtime(bu))
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("\U0001F4E9 Ajukan Banding", callback_data="appeal:ajukan")]])
    await context.bot.send_message(
        update.effective_chat.id,
        ft(f"\U0001F6AB *Kamu di-ban{' permanen' if bu == -1 else ' sementara'}*\n\n"
           + (f"\u23f3 Sampai: *{kapan}*\n" if bu != -1 else "")
           + f"\u26d4 Alasan: {d.get('ban_reason') or '-'}\n\n"
           "Kalau merasa ini salah, ajukan banding ke admin \U0001F64F"),
        parse_mode="Markdown", reply_markup=kb)
    return True
def _mode_admin_aktif(update: Update) -> bool:
    s = sessions.get(update.effective_user.id, {})
    return bool(s.get("mode_admin"))


async def _boleh_masuk_produksi(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                s: dict, chat_id: int) -> bool:
    """Ban check + anti-spam flow2api + cek proses aktif. True kalau boleh lanjut produksi."""
    if await tolak_kalau_ban(update, context):
        return False

    uid = update.effective_user.id

    # 1. Anti double-click tombol
    if is_spam_click(uid, 2.0):
        if update.callback_query:
            try:
                await update.callback_query.answer("⚠️ Tombol sudah ditekan sebelumnya, mohon tunggu sebentar...", show_alert=True)
            except Exception:
                pass
        return False

    # 2. Cek apakah user sedang memiliki proses aktif yang belum selesai
    aktif = proses_user_aktif(uid)
    if aktif:
        tipe, pesan = aktif
        popup_pesan = "⚠️ Masih ada proses yang belum selesai, tunggu dulu!"
        if tipe in ("video_generating", "antrean_waiting"):
            pos = antrean.posisi_antrean(uid)
            pos_txt = f" (Posisi antrean #{pos})" if pos > 0 else ""
            popup_pesan = f"⚠️ Masih ada video yang sedang diproses{pos_txt}! Tunggu sampai selesai ya."
        elif tipe == "storyboard_producing":
            popup_pesan = "⚠️ Masih ada proses storyboard yang sedang berjalan! Tunggu sebentar ya."
        if update.callback_query:
            try:
                await update.callback_query.answer(popup_pesan, show_alert=True)
            except Exception:
                pass
        await say(context, chat_id, s, pesan, fresh=True)
        return False

    ok, msg = boleh_produksi(uid)
    if not ok:
        await say(context, chat_id, s, msg, fresh=True)
        return False
    catat_produksi(uid)
    return True

if __name__ == "__main__":
    main()
