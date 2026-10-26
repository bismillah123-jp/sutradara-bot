"""Voiceover Bahasa Indonesia & English via edge-tts + FFmpeg video dubbing."""
import asyncio
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Default voices
VOICE_ID_FEMALE = "id-ID-GadisNeural"
VOICE_ID_MALE = "id-ID-ArdiNeural"
VOICE_EN = "en-US-AvaNeural"
VOICE = VOICE_ID_FEMALE


async def make_voiceover(text: str, out_path: str, voice: str = VOICE, rate: str = "+5%", max_retries: int = 3):
    """Generate high-quality neural voiceover using edge-tts with retry & voice fallback."""
    try:
        import edge_tts
    except ImportError:
        log.warning("edge-tts belum terinstall. Voiceover tidak dapat dibuat.")
        raise RuntimeError("edge-tts tidak ditemukan di sistem.")

    last_err = None
    voices_to_try = [voice]
    if voice == VOICE_ID_FEMALE:
        voices_to_try.append(VOICE_ID_MALE)
    elif voice == VOICE_ID_MALE:
        voices_to_try.append(VOICE_ID_FEMALE)

    for v in voices_to_try:
        for attempt in range(1, max_retries + 1):
            try:
                communicate = edge_tts.Communicate(text, v, rate=rate)
                await communicate.save(out_path)
                if Path(out_path).exists() and Path(out_path).stat().st_size > 0:
                    return out_path
            except Exception as e:
                last_err = e
                log.warning("edge-tts attempt %d failed for voice %s: %s", attempt, v, e)
                await asyncio.sleep(1)

    if last_err:
        raise last_err
    raise RuntimeError("Gagal membuat voiceover.")


def has_audio_stream(video_path: str) -> bool:
    """Check if video has an audio stream."""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            str(video_path)
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return bool(r.stdout.strip())
    except Exception:
        return False


def mix_voiceover_to_video(video_path: str, vo_audio_path: str, out_path: str) -> bool:
    """Mix voiceover audio with video using ffmpeg.
    If video has background audio (foley/ambience), it mixes with volume 0.35 bg + 1.0 vo.
    If video is silent, it directly muxes the voiceover audio.
    """
    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg tidak ditemukan di PATH, mixing audio dilewati.")
        return False

    v_path = Path(video_path)
    a_path = Path(vo_audio_path)
    o_path = Path(out_path)

    if not v_path.exists() or not a_path.exists():
        log.warning("File input untuk mixing tidak ditemukan: %s atau %s", v_path, a_path)
        return False

    try:
        if has_audio_stream(str(v_path)):
            # Mix existing video foley (35% volume) with crystal clear VO (100% volume)
            cmd = [
                "ffmpeg", "-y",
                "-i", str(v_path),
                "-i", str(a_path),
                "-filter_complex",
                "[0:a]volume=0.35[bg];[1:a]volume=1.0[vo];[bg][vo]amix=inputs=2:duration=first:dropout_transition=2[aout]",
                "-map", "0:v",
                "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                str(o_path)
            ]
        else:
            # Silent video: attach VO directly
            cmd = [
                "ffmpeg", "-y",
                "-i", str(v_path),
                "-i", str(a_path),
                "-map", "0:v",
                "-map", "1:a",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                str(o_path)
            ]

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and o_path.exists() and o_path.stat().st_size > 0:
            log.info("Voiceover berhasil di-mix ke video: %s (size: %d bytes)",
                     o_path.name, o_path.stat().st_size)
            return True
        log.warning("ffmpeg mixing gagal (code %d): %s", r.returncode, r.stderr[:300])
        return False
    except Exception as e:
        log.warning("mix_voiceover_to_video exception: %s", e)
        return False
