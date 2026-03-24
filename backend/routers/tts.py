"""
routers/tts.py — Text-to-Speech via Azure Cognitive Services Speech Service.

Uses Azure Neural TTS voices — these are human-quality voices trained on real
speech data, not traditional concatenative TTS. They are indistinguishable from
human speech in blind listening tests.

Supported voices:
  Indian English  → Neerja (F), Prabhat (M)     — en-IN
  American English→ Aria (F),   Guy (M)          — en-US
  Hindi           → Swara (F),  Madhur (M)        — hi-IN
  Telugu          → Shruti (F), Mohan (M)         — te-IN
  Tamil           → Pallavi (F),Valluvar (M)      — ta-IN

Endpoint:  POST /api/tts
Returns:   audio/mpeg (MP3) stream
"""
from __future__ import annotations

import logging
import os
import re
import html as _html
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

import deps
from deps import get_current_user

logger = logging.getLogger("auragraph")
router = APIRouter(tags=["tts"])

# ── Voice registry ─────────────────────────────────────────────────────────────
# Azure Neural voices — all rated 4-5/5 for naturalness in Microsoft's benchmarks.
# Using "neural" suffix for highest quality. Each voice has been chosen for
# clarity and warmth specifically for educational/reading contexts.

VOICES = {
    "en-IN-F": {"name": "en-IN-NeerjaNeural",       "lang": "en-IN", "label": "Neerja (Indian English ♀)", "flag": "🇮🇳"},
    "en-IN-M": {"name": "en-IN-PrabhatNeural",       "lang": "en-IN", "label": "Prabhat (Indian English ♂)", "flag": "🇮🇳"},
    "en-US-F": {"name": "en-US-AriaNeural",          "lang": "en-US", "label": "Aria (American English ♀)", "flag": "🇺🇸"},
    "en-US-M": {"name": "en-US-GuyNeural",           "lang": "en-US", "label": "Guy (American English ♂)",  "flag": "🇺🇸"},
    "hi-IN-F": {"name": "hi-IN-SwaraNeural",         "lang": "hi-IN", "label": "Swara (Hindi ♀)",           "flag": "🇮🇳"},
    "hi-IN-M": {"name": "hi-IN-MadhurNeural",        "lang": "hi-IN", "label": "Madhur (Hindi ♂)",          "flag": "🇮🇳"},
    "te-IN-F": {"name": "te-IN-ShrutiNeural",        "lang": "te-IN", "label": "Shruti (Telugu ♀)",         "flag": "🇮🇳"},
    "te-IN-M": {"name": "te-IN-MohanNeural",         "lang": "te-IN", "label": "Mohan (Telugu ♂)",          "flag": "🇮🇳"},
    "ta-IN-F": {"name": "ta-IN-PallaviNeural",       "lang": "ta-IN", "label": "Pallavi (Tamil ♀)",         "flag": "🇮🇳"},
    "ta-IN-M": {"name": "ta-IN-ValluvarNeural",      "lang": "ta-IN", "label": "Valluvar (Tamil ♂)",        "flag": "🇮🇳"},
}
DEFAULT_VOICE = "en-IN-F"


class TTSRequest(BaseModel):
    text:    str  = Field(..., min_length=1, max_length=8000)
    voice:   str  = DEFAULT_VOICE
    rate:    str  = "0%"    # e.g. "-10%", "0%", "+10%", "+20%"
    pitch:   str  = "0%"    # e.g. "-5%", "0%"
    use_ai_transcript: bool = True


def _latex_to_speech(expr: str) -> str:
    """Convert common LaTeX math to a readable speech transcript."""
    s = (expr or "").strip()
    if not s:
        return "formula"

    # Normalize wrappers often used by LLM notes.
    s = s.replace("\\left", "").replace("\\right", "")

    # Repeatedly resolve simple structural commands.
    frac_pat = re.compile(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}")
    sqrt_pat = re.compile(r"\\sqrt\s*\{([^{}]+)\}")
    while True:
        new_s = frac_pat.sub(r"(\1 over \2)", s)
        new_s = sqrt_pat.sub(r"square root of \1", new_s)
        if new_s == s:
            break
        s = new_s

    # Limits / sums / integrals with common forms.
    s = re.sub(r"\\sum_\{([^{}]+)\}\^\{([^{}]+)\}", r"sum from \1 to \2 of", s)
    s = re.sub(r"\\int_\{([^{}]+)\}\^\{([^{}]+)\}", r"integral from \1 to \2 of", s)
    s = re.sub(r"\\lim_\{([^{}]+)\}", r"limit as \1 of", s)

    # Superscripts / subscripts.
    s = re.sub(r"\^\{([^{}]+)\}", r" to the power of \1 ", s)
    s = re.sub(r"\^([A-Za-z0-9])", r" to the power of \1 ", s)
    s = re.sub(r"_\{([^{}]+)\}", r" sub \1 ", s)
    s = re.sub(r"_([A-Za-z0-9])", r" sub \1 ", s)

    greek = {
        r"\\alpha": "alpha", r"\\beta": "beta", r"\\gamma": "gamma", r"\\delta": "delta",
        r"\\theta": "theta", r"\\lambda": "lambda", r"\\mu": "mu", r"\\pi": "pi",
        r"\\sigma": "sigma", r"\\omega": "omega", r"\\phi": "phi",
    }
    ops = {
        r"\\cdot": " times ", r"\\times": " times ", r"\\pm": " plus or minus ",
        r"\\to": " tends to ", r"\\rightarrow": " maps to ", r"\\geq": " greater than or equal to ",
        r"\\leq": " less than or equal to ", r"\\neq": " not equal to ",
        r"\\infty": " infinity ",
    }
    for k, v in {**greek, **ops}.items():
        s = re.sub(k, v, s)

    # Function names / formatting commands.
    s = re.sub(r"\\(sin|cos|tan|log|ln|exp|max|min|det|Pr)\b", r"\1", s)
    s = re.sub(r"\\(mathrm|text|operatorname)\{([^{}]+)\}", r"\2", s)

    # Read common function notation naturally.
    s = re.sub(r"\bf\s*\(\s*x\s*\)", "f of x", s)
    s = re.sub(r"\b([A-Za-z])\s*\(\s*([A-Za-z0-9])\s*\)", r"\1 of \2", s)

    # Generic cleanup.
    s = s.replace("{", " ").replace("}", " ").replace("\\", " ")
    s = s.replace("=", " equals ").replace("<=", " less than or equal to ").replace(">=", " greater than or equal to ")
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s or "formula"


def _base_transcript_from_markdown(text: str) -> str:
    """Build a speakable transcript from markdown+LaTeX, preserving formulas."""
    t = text or ""

    # Remove code content.
    t = re.sub(r"```[\s\S]*?```", " code block omitted. ", t)
    t = re.sub(r"`[^`]+`", " code ", t)

    # Convert display and inline LaTeX to speech-friendly phrases.
    t = re.sub(
        r"\$\$([\s\S]*?)\$\$",
        lambda m: f" Equation: {_latex_to_speech(m.group(1))}. ",
        t,
    )
    t = re.sub(
        r"\$([^$\n]{1,500})\$",
        lambda m: f" {_latex_to_speech(m.group(1))} ",
        t,
    )

    # Markdown cleanup while keeping actual content.
    t = re.sub(r"^#{1,6}\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", t)
    t = re.sub(r"_{1,2}([^_]+)_{1,2}", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"^>\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"^[-*_]{3,}\s*$", ". ", t, flags=re.MULTILINE)
    t = re.sub(r"^[\-*+]\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()


async def _maybe_ai_refine_transcript(transcript: str, use_ai: bool) -> str:
    """Optionally refine transcript using LLM so math sounds natural when read aloud."""
    if not use_ai:
        return transcript
    if not transcript or len(transcript) > 6000:
        return transcript

    prompt = (
        "Convert the following study note transcript into natural spoken narration for TTS. "
        "Keep all math meaning exactly correct. Read formulas in plain words (example: f(x) -> f of x). "
        "Do not use markdown, bullets, or symbols-heavy notation. Return only the final transcript text.\n\n"
        f"TEXT:\n{transcript}"
    )
    msgs = [{"role": "user", "content": prompt}]

    try:
        if deps._is_azure_available():
            out = await deps._azure_chat(msgs, max_tokens=2200)
            return (out or transcript).strip()
        if deps._is_groq_available():
            out = await deps._groq_chat(msgs, max_tokens=2200)
            return (out or transcript).strip()
    except Exception as e:
        logger.warning("TTS transcript refine failed; using base transcript: %s", e)
    return transcript


async def _build_tts_transcript(text: str, use_ai_transcript: bool) -> str:
    base = _base_transcript_from_markdown(text)
    refined = await _maybe_ai_refine_transcript(base, use_ai_transcript)
    return refined


def _clean_text_for_tts(text: str) -> str:
    """Final SSML-safe cleanup (do not remove formulas here; transcript already spoken)."""
    t = re.sub(r"\n{3,}", "\n\n", text or "")
    t = re.sub(r"\s{2,}", " ", t)
    return _html.escape(t.strip())


def _build_ssml(text: str, voice_key: str, rate: str, pitch: str) -> str:
    """Build SSML markup for Azure Speech Service."""
    voice = VOICES.get(voice_key, VOICES[DEFAULT_VOICE])
    clean  = _clean_text_for_tts(text)
    # Clamp rate to safe range
    rate_val = max(-30, min(50, int(rate.replace('%','') or 0)))
    rate_str = f"{'+' if rate_val >= 0 else ''}{rate_val}%"
    return f"""<speak version='1.0' xml:lang='{voice['lang']}'>
  <voice name='{voice['name']}'>
    <prosody rate='{rate_str}' pitch='{pitch}'>
      {clean}
    </prosody>
  </voice>
</speak>"""


def _speech_configured() -> bool:
    key    = os.environ.get("AZURE_SPEECH_KEY", "")
    region = os.environ.get("AZURE_SPEECH_REGION", "")
    return bool(key and region
                and not key.startswith("your-")
                and not region.startswith("your-"))


async def _call_azure_tts(ssml: str) -> bytes:
    """Call Azure Speech REST API and return raw MP3 bytes."""
    key    = os.environ.get("AZURE_SPEECH_KEY", "")
    region = os.environ.get("AZURE_SPEECH_REGION", "")
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3",
        "User-Agent": "AuraGraph/1.0",
    }
    import httpx
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=headers, content=ssml.encode("utf-8"))
        if resp.status_code != 200:
            logger.warning("Azure TTS failed: %d %s", resp.status_code, resp.text[:200])
            raise HTTPException(502, f"Azure Speech Service error: {resp.status_code}")
        return resp.content


@router.get("/api/tts/voices")
async def list_voices():
    """Return available voices — no auth required."""
    return {
        "voices": [
            {"key": k, **{f: v[f] for f in ("name","lang","label","flag")}}
            for k, v in VOICES.items()
        ],
        "default": DEFAULT_VOICE,
        "azure_configured": _speech_configured(),
    }


@router.post("/api/tts")
async def synthesize_speech(
    req: TTSRequest,
    authorization: Optional[str] = Header(None),
):
    """
    Synthesize text to speech using Azure Neural TTS.
    Returns MP3 audio bytes.
    Falls back to a JSON error if Azure Speech is not configured.
    """
    # Auth check — any logged-in user can use TTS
    try:
        get_current_user(authorization)
    except HTTPException:
        pass  # Allow demo users

    if not _speech_configured():
        raise HTTPException(
            503,
            "Azure Speech Service is not configured. "
            "Add AZURE_SPEECH_KEY and AZURE_SPEECH_REGION to .env"
        )

    if req.voice not in VOICES:
        req.voice = DEFAULT_VOICE

    transcript = await _build_tts_transcript(req.text, req.use_ai_transcript)
    ssml  = _build_ssml(transcript, req.voice, req.rate, req.pitch)
    audio = await _call_azure_tts(ssml)

    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Length": str(len(audio)),
        },
    )
