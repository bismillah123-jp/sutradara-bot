"""Rakit video final: scene + voiceover + subtitle -> MP4 9:16."""
import subprocess
from pathlib import Path

W, H = 1080, 1920


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg gagal: {r.stderr[-500:]}")


def probe_duration(path: str) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", path],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 5.0


def _ts(sec: float) -> str:
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    ms = int((s % 1) * 1000)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"


def assemble_video(scenes: list, out_path: str):
    tmp = Path(out_path).parent
    segs = []
    for i, sc in enumerate(scenes):
        seg = tmp / f"seg{i+1}.mp4"
        dur = max(2.0, probe_duration(sc["audio_path"]) + 0.4)
        _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
              "-stream_loop", "2", "-i", sc["video_path"], "-i", sc["audio_path"],
              "-filter_complex",
              f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps=30[v]",
              "-map", "[v]", "-map", "1:a", "-t", f"{dur:.2f}",
              "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-c:a", "aac",
              "-shortest", str(seg)])
        segs.append(seg)
    lst = tmp / "concat.txt"
    lst.write_text("".join(f"file '{s}'\n" for s in segs))
    joined = tmp / "joined.mp4"
    _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
          "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)])
    srt = tmp / "subs.srt"
    t, lines = 0.0, []
    for i, sc in enumerate(scenes):
        dur = max(2.0, probe_duration(sc["audio_path"]) + 0.4)
        lines.append(f"{i+1}\n{_ts(t)} --> {_ts(t+dur)}\n{sc['narasi']}\n")
        t += dur
    srt.write_text("\n".join(lines), encoding="utf-8")
    _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(joined),
          "-vf", "subtitles=" + str(srt) + ":force_style='FontName=DejaVu Sans,FontSize=22,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=1,Outline=2,MarginV=120'",
          "-c:a", "copy", out_path])
