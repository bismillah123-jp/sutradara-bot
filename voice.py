"""Voiceover Bahasa Indonesia via edge-tts."""
import edge_tts

VOICE = "id-ID-ArdiNeural"


async def make_voiceover(text: str, out_path: str, voice: str = VOICE):
    await edge_tts.Communicate(text, voice, rate="+5%").save(out_path)
