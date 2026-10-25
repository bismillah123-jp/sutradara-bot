"""Client flow2api v2 - gambar (dengan referensi), enhance, video.

MIGRASI VEO 3.1 FAST:
- generate_video memakai veo-3.1-fast-r2v-8s-{portrait|landscape}
  (r2v = reference-to-video, ingredients maks 3 gambar, min 1).
- Durasi 8/16/24 detik -> 1/2/3 segmen @ 8 detik (generate_video_multi).
- Frame chaining: frame terakhir segmen N diekstrak via ffmpeg, dikirim
  sebagai ingredient tambahan utk segmen N+1 (fallback first-frame).
- Stitching: ffmpeg concat demuxer + copy codec (tanpa re-encode).
- Katalog HD (hasil enhance) SELALU ingredient #1 di setiap generate.
- Setiap segmen: prompt + PRODUCT SPEC marker + consistency checklist.
"""
import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from typing import List, Optional


def _mime(path: str) -> str:
    p = path.lower()
    if p.endswith(".png"):
        return "image/png"
    if p.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


VEO_SEGMENT_SECONDS = 8
VEO_MAX_IMAGES = 3
VEO_MODEL_TMPL = "veo-3.1-fast-r2v-8s-{ratio}"
VEO_MODEL_T2V_TMPL = "veo-3.1-fast-8s-{ratio}"


def veo_model_name(aspect="9:16", with_images=True):
    ratio = "portrait" if aspect == "9:16" else "landscape"
    tmpl = VEO_MODEL_TMPL if with_images else VEO_MODEL_T2V_TMPL
    return tmpl.format(ratio=ratio)


def segments_for_seconds(seconds):
    return max(1, int(round(max(1, int(seconds)) / VEO_SEGMENT_SECONDS)))


def _have_ffmpeg():
    return shutil.which("ffmpeg") is not None


def _run(cmd):
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)


def extract_last_frame(video_path, out_path):
    _run(["ffmpeg", "-y", "-sseof", "-0.5", "-i", video_path,
          "-vframes", "1", "-q:v", "2", out_path])
    return out_path


def stitch_mp4(parts, out_path):
    lst = os.path.join(os.path.dirname(out_path) or ".",
                       "concat_%d.txt" % os.getpid())
    with open(lst, "w") as f:
        for p in parts:
            # Quote paths to handle spaces in filenames
            f.write("file '" + p.replace("'", "'\\''") + "'\n")
    try:
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
              "-i", lst, "-c", "copy", out_path])
    finally:
        try:
            os.remove(lst)
        except OSError:
            pass
    return out_path


def with_consistency(prompt, segment=0, total=1):
    """Ensure seamless continuity for multi-segment videos without prompt pollution."""
    if total <= 1 or segment <= 1 or "CONTINUITY:" in prompt:
        return prompt
    continuity_tag = (
        "CONTINUITY: Seamless visual and physical continuity from the attached reference frame. "
        "Maintain identical character, outfit, product position, background environment, and lighting. "
        "Continue the action forward naturally without restarting or repeating earlier scenes.\n\n"
    )
    return f"{continuity_tag}{prompt}"


def _image_model(aspect):
    m = {"9:16": "gemini-3.1-flash-image-portrait",
         "16:9": "gemini-3.1-flash-image-landscape",
         "1:1": "gemini-3.1-flash-image-square",
         "4:3": "gemini-3.1-flash-image-four-three",
         "3:4": "gemini-3.1-flash-image-three-four"}
    return m.get(aspect, "gemini-3.1-flash-image-portrait")


class FlowClient:
    def __init__(self, base, api_key):
        self.base = base.rstrip("/")
        self.key = api_key

    def _post(self, path, body, timeout=180):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.key})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    @staticmethod
    def _download(url, dest):
        with urllib.request.urlopen(url, timeout=120) as r:
            data = r.read()
        with open(dest, "wb") as f:
            f.write(data)
        return dest

    def _resolve_images(self, paths, workdir):
        local = []
        for i, p in enumerate(paths or []):
            p = str(p)
            if p.startswith("http://") or p.startswith("https://"):
                ext = ".jpg"
                pl = p.lower()
                if ".png" in pl:
                    ext = ".png"
                elif ".webp" in pl:
                    ext = ".webp"
                dest = os.path.join(workdir, "ingredient_dl_%d%s" % (i, ext))
                local.append(self._download(p, dest))
            elif os.path.isfile(p):
                local.append(p)
        return local

    def _ingredients(self, frame_paths, reference_images, workdir):
        ordered = []
        for p in (reference_images or []) + (frame_paths or []):
            if p and p not in ordered:
                ordered.append(p)
        ordered = ordered[:VEO_MAX_IMAGES]
        return self._resolve_images(ordered, workdir)

    def _video_parts(self, prompt, image_paths):
        parts = [{"text": prompt}]
        for fp in image_paths:
            with open(fp, "rb") as f:
                parts.append({"inlineData": {
                    "mimeType": _mime(fp),
                    "data": base64.b64encode(f.read()).decode()}})
        return parts

    @staticmethod
    def _video_uri(res):
        cparts = res["candidates"][0]["content"]["parts"]
        f = next(p for p in cparts
                 if (p.get("fileData") or {}).get("fileUri")
                 or (p.get("file_data") or {}).get("file_uri"))
        return ((f.get("fileData") or {}).get("fileUri")
                or f["file_data"]["file_uri"])

    async def generate_video(self, prompt, frame_paths, seconds=8,
                 aspect="9:16", reference_images=None,
                 workdir=None, on_segment=None):
        segments = segments_for_seconds(seconds)
        if segments == 1:
            if on_segment:
                r = on_segment(1, 1)
                if asyncio.iscoroutine(r):
                    await r
            wd = workdir or tempfile.mkdtemp(prefix="veo_")
            p = prompt[0] if isinstance(prompt, (list, tuple)) else str(prompt)
            refs = self._ingredients(frame_paths, reference_images, wd)
            model = veo_model_name(aspect, with_images=bool(refs))
            parts = self._video_parts(p, refs)
            data = {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"responseModalities": ["VIDEO"]},
            }
            body = await asyncio.wait_for(
                asyncio.to_thread(
                    self._post,
                    "/v1beta/models/%s:generateContent" % model,
                    data, 300),
                timeout=330)
            uri = self._video_uri(body)
            if not uri:
                raise RuntimeError("veo: no video uri")
            out = os.path.join(wd, "single.mp4")
            await asyncio.to_thread(self._download, uri, out)
            with open(out, "rb") as f:
                return f.read()
        return await self.generate_video_multi(
            prompt, frame_paths, segments=segments, aspect=aspect,
            reference_images=reference_images, workdir=workdir,
            on_segment=on_segment)

    async def generate_video_multi(self, prompt, frame_paths, segments=1,
                                   aspect="9:16", reference_images=None,
                                   workdir=None, on_segment=None):
        wd = workdir or tempfile.mkdtemp(prefix="veo_seg_")
        catalog_refs = [p for p in (reference_images or []) if p]
        catalog_hd = catalog_refs[0] if catalog_refs else None
        frames = [str(p) for p in (frame_paths or []) if p]

        prompt_list = list(prompt) if isinstance(prompt, (list, tuple)) else [str(prompt)] * segments

        seg_files = []
        chain_frame = None
        for i in range(1, segments + 1):
            if on_segment:
                r = on_segment(i, segments)
                if asyncio.iscoroutine(r):
                    await r
            seg_p = prompt_list[i - 1] if i - 1 < len(prompt_list) else prompt_list[-1]
            seg_prompt = with_consistency(seg_p, i, segments)

            # Build ingredient images for this segment (up to VEO_MAX_IMAGES = 3):
            # 1. Catalog HD is ALWAYS reference #1 (product fidelity)
            # 2. For segment > 1: chain_frame (last frame of previous segment) is reference #2
            # 3. Targeted storyboard frame for this segment as reference #3
            seg_refs = []
            if catalog_hd:
                seg_refs.append(catalog_hd)

            if i > 1 and chain_frame and os.path.exists(chain_frame):
                seg_refs.append(chain_frame)

            # Assign relevant frame(s) for this segment
            frame_idx = (i - 1) * 2
            cand_frames = []
            if frame_idx < len(frames):
                cand_frames.append(frames[frame_idx])
            if frame_idx + 1 < len(frames):
                cand_frames.append(frames[frame_idx + 1])
            for cf in cand_frames + frames:
                if len(seg_refs) >= VEO_MAX_IMAGES:
                    break
                if cf and cf not in seg_refs:
                    seg_refs.append(cf)

            seg_bytes = await self.generate_video(
                seg_prompt, [], seconds=VEO_SEGMENT_SECONDS,
                aspect=aspect, reference_images=seg_refs, workdir=wd)
            seg_path = os.path.join(wd, "seg_%02d.mp4" % i)
            with open(seg_path, "wb") as f:
                f.write(seg_bytes)
            seg_files.append(seg_path)
            if i < segments:
                chain_frame = os.path.join(wd, "chain_%02d.jpg" % i)
                await asyncio.to_thread(extract_last_frame,
                                        seg_path, chain_frame)
        if segments == 1:
            with open(seg_files[0], "rb") as f:
                return f.read()
        out = os.path.join(wd, "final_stitched.mp4")
        await asyncio.to_thread(stitch_mp4, seg_files, out)
        with open(out, "rb") as f:
            return f.read()

    async def gen_frames(self, prompt, count, refs,
                         style="cinematic photo", aspect="9:16"):
        outs = []
        for i in range(count):
            p = "%s, %s, frame %d, vertical %s" % (style, prompt, i + 1, aspect)
            parts = [{"text": p}]
            for rp in refs[:2]:
                with open(rp, "rb") as f:
                    parts.append({"inlineData": {
                        "mimeType": _mime(rp),
                        "data": base64.b64encode(f.read()).decode()}})
            res = await asyncio.wait_for(
                asyncio.to_thread(self._post,
                    "/v1beta/models/%s:generateContent" % _image_model(aspect),
                    {"contents": [{"parts": parts}]}, 120),
                timeout=180)
            data = next(p["inlineData"]["data"]
                        for p in res["candidates"][0]["content"]["parts"]
                        if "inlineData" in p)
            out = "/tmp/frame_%d_%d.jpg" % (i, os.getpid())
            with open(out, "wb") as f:
                f.write(base64.b64decode(data))
            outs.append(out)
        return outs

    async def gen_hd(self, path):
        with open(path, "rb") as f:
            parts = [{"text": "enhance to HD, keep identity"},
                     {"inlineData": {"mimeType": _mime(path),
                      "data": base64.b64encode(f.read()).decode()}}]
        res = await asyncio.wait_for(
            asyncio.to_thread(self._post,
                "/v1beta/models/gemini-3.1-flash-image-portrait:generateContent",
                {"contents": [{"parts": parts}]}, 120),
            timeout=180)
        data = next(p["inlineData"]["data"]
                    for p in res["candidates"][0]["content"]["parts"]
                    if "inlineData" in p)
        out = "/tmp/hd_%d.jpg" % os.getpid()
        with open(out, "wb") as f:
            f.write(base64.b64decode(data))
        return out

    async def generate_image(self, prompt, aspect="9:16", ref_paths=None, timeout=300):
        parts = [{"text": prompt}]
        for rp in (ref_paths or []):
            with open(rp, "rb") as f:
                parts.append({"inlineData": {
                    "mimeType": _mime(rp),
                    "data": base64.b64encode(f.read()).decode()}})
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(self._post,
                    "/v1beta/models/%s:generateContent" % _image_model(aspect),
                    {"contents": [{"parts": parts}]}, timeout),
                timeout=timeout)
            data = next(p["inlineData"]["data"]
                        for p in res["candidates"][0]["content"]["parts"]
                        if "inlineData" in p)
            return base64.b64decode(data)
        except Exception:
            return None
