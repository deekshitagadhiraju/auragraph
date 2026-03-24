"""routers/feedback.py — Student feedback collection + admin read."""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

import deps

logger = logging.getLogger("auragraph")
router = APIRouter(tags=["feedback"])


class FeedbackRequest(BaseModel):
    context:     str = "dashboard"        # 'dashboard' | 'notebook'
    notebook_id: Optional[str] = None
    rating:      Optional[int] = Field(default=None, ge=1, le=5)
    liked:       str = ""
    disliked:    str = ""
    category:    str = "general"          # 'notes' | 'questions' | 'mutation' | 'ui' | 'general'
    message:     str = Field(default="", max_length=2000)
    page_url:    str = ""


class HelpChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=800)
    page_url: str = ""


class HelpChatResponse(BaseModel):
    answer: str
    source: str = "local"


_HELP_WEBSITE_CONTEXT = """
You are AuraGraph Navigation Assistant.

App structure and key actions:
- Dashboard: Create notebook, open courses/notebooks, review progress cards.
- Notebook page top actions: Ask A Doubt, History, Edit Notes, Annotate, Quick Review, Keyboard Shortcuts, On-Screen Keyboard.
- Ask A Doubt flow: click Ask A Doubt -> type question -> Ask (Get Answer) -> answer logs in Doubts panel.
- Mutate flow: Ask A Doubt modal has Rewrite Page.
- Quick Review: opens short AI summary panel for notes.
- Study Hub side panel: Knowledge Map (graph + quizzes) and Doubts log.
- Annotation tools: highlight, sticky note, draw, eraser, save/clear.
""".strip()


def _local_help_fallback(question: str) -> str:
    q = (question or "").lower()
    if any(k in q for k in ["ask doubt", "ask a doubt", "doubt panel", "how to doubt"]):
        return "1. Open the notebook.\n2. Click Ask A Doubt.\n3. Type your question and click Ask (Get Answer).\n4. Check the Doubts panel for the saved answer."
    if any(k in q for k in ["mutate", "rewrite page", "regenerate"]):
        return "1. Open Ask A Doubt.\n2. Use Rewrite Page.\n3. Review the changed page content."
    if "quick review" in q or "cheatsheet" in q:
        return "Click Quick Review in the notebook toolbar to open the concise summary panel."
    if q.strip().startswith(("is ", "can ", "do ", "does ", "should ", "will ")):
        return "Yes, if you are on the notebook page where that action is available in the top toolbar."
    return "I can help with Ask A Doubt, Mutate/Rewrite Page, Quick Review, Annotation tools, and Study Hub navigation. Ask one specific action."


def _compact_help_answer(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return "Please ask one specific navigation question."
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    if len(lines) > 5:
        lines = lines[:5]
    out = "\n".join(lines)
    words = out.split()
    if len(words) > 90:
        out = " ".join(words[:90]).rstrip(" ,.;") + "..."
    return out


async def _send_webhook(entry: dict):
    """Fire-and-forget Discord/Slack webhook so feedback reaches developers instantly."""
    url = os.environ.get("FEEDBACK_WEBHOOK_URL", "")
    if not url:
        return
    try:
        import httpx
        stars = "⭐" * (entry.get("rating") or 0)
        text = (
            f"**New AuraGraph Feedback** {stars}\n"
            f"• Context: `{entry.get('context','?')}` | "
            f"Category: `{entry.get('category','?')}`\n"
            f"• User: `{entry.get('user_email') or entry.get('user_id','anonymous')}`\n"
        )
        if entry.get("liked"):
            text += f"• 👍 Liked: {entry['liked']}\n"
        if entry.get("disliked"):
            text += f"• 👎 Disliked: {entry['disliked']}\n"
        if entry.get("message"):
            text += f"• Message: {entry['message']}\n"
        # Discord payload
        payload = {"content": text[:1900]}
        async with httpx.AsyncClient(timeout=8.0) as client:
            await client.post(url, json=payload)
    except Exception as exc:
        logger.warning("feedback webhook failed: %s", exc)


@router.post("/api/feedback")
async def submit_feedback(
    req: FeedbackRequest,
    authorization: Optional[str] = Header(None),
):
    """Submit feedback — available to all authenticated users."""
    from agents.notebook_store import save_feedback
    try:
        user = deps.get_current_user(authorization)
    except HTTPException:
        user = {"id": "anonymous", "email": ""}

    entry = req.model_dump()
    entry["user_id"]    = user.get("id", "anonymous")
    entry["user_email"] = user.get("email", "")

    fid = save_feedback(entry)

    # Async webhook — don't await so response is instant
    import asyncio
    asyncio.ensure_future(_send_webhook(entry))

    return {"ok": True, "id": fid}


@router.post("/api/help-chat", response_model=HelpChatResponse)
async def help_chat(
    req: HelpChatRequest,
    authorization: Optional[str] = Header(None),
):
    """LLM-backed navigation helper with strict short-answer behavior."""
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(400, "Question is required.")

    try:
        user = deps.get_current_user(authorization)
    except HTTPException:
        user = {"id": "anonymous", "email": ""}

    user_id = user.get("id", "anonymous")
    source = "local"

    system_prompt = (
        "You are a website navigation helper for AuraGraph. "
        "Answer very briefly and simply. "
        "Rules: "
        "(1) For how-to questions, give 2-4 short numbered steps only. "
        "(2) For yes/no questions, start with Yes or No, then one short reason. "
        "(3) Keep response under 80 words. "
        "(4) No extra explanations, no long paragraphs. "
        "(5) If unsure, ask one clarifying question in one line."
    )
    user_prompt = (
        f"Website context:\n{_HELP_WEBSITE_CONTEXT}\n\n"
        f"Current page URL: {req.page_url or 'unknown'}\n"
        f"User question: {question}"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    answer: Optional[str] = None
    if deps._is_azure_available() or deps._is_groq_available():
        try:
            if user_id != "anonymous":
                deps._check_llm_rate_limit(user_id)

            if deps._is_azure_available():
                try:
                    answer = await deps._azure_chat(messages, max_tokens=120)
                    source = "azure"
                except Exception as exc:
                    logger.warning("help-chat azure failed: %s", exc)

            if answer is None and deps._is_groq_available():
                try:
                    answer = await deps._groq_chat(messages, max_tokens=120)
                    source = "groq"
                except Exception as exc:
                    logger.warning("help-chat groq failed: %s", exc)

            if answer and user_id != "anonymous":
                deps._record_llm_call(user_id, source, est_tokens=240)
        except Exception as exc:
            logger.warning("help-chat llm path failed: %s", exc)

    if not answer:
        answer = _local_help_fallback(question)
        source = "local"

    return HelpChatResponse(answer=_compact_help_answer(answer), source=source)


@router.get("/api/feedback")
async def get_feedback(
    authorization: Optional[str] = Header(None),
    limit: int = 200,
):
    """Admin endpoint — protected by ADMIN_KEY env var."""
    admin_key = os.environ.get("ADMIN_KEY", "")
    # Accept admin_key passed as Bearer token OR as query param
    token = ""
    if authorization:
        token = authorization.replace("Bearer ", "").strip()
    if admin_key and token != admin_key:
        raise HTTPException(403, "Admin access required.")
    if not admin_key:
        raise HTTPException(503, "ADMIN_KEY not configured on server.")
    from agents.notebook_store import get_all_feedback
    rows = get_all_feedback(limit=limit)
    return {"feedback": rows, "total": len(rows)}
