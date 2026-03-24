"""routers/shortnotes.py — AI-generated cheatsheet / summary notes per notebook."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import deps
from deps import get_current_user, _require_notebook_owner, _check_llm_rate_limit, _record_llm_call

logger = logging.getLogger("auragraph")
router = APIRouter(tags=["shortnotes"])


_SHORT_NOTES_SYSTEM = """\
You are AuraGraph's Cheatsheet Engine — a concise, exam-focused note synthesiser.

You receive a student's full notes, their highlights, doubts they raised, their
mastery status per concept, and their proficiency level.

Your job: generate a PERFECT one-page cheatsheet the student can review in 10 minutes
before an exam. This is NOT a summary — it is an intelligent distillation that:

1. Prioritises what the student found difficult (struggling concepts, raised doubts)
2. Reinforces what they highlighted as important
3. Includes every essential formula, definition, and diagram description
4. Skips verbose explanations — use dense, tight bullet points
5. Calibrates depth to their proficiency level

OUTPUT FORMAT (strict Markdown, no deviations):
# ⚡ Quick Review: {notebook_name}
*{proficiency} level · {concept_count} concepts*

## 🔑 Key Concepts & Definitions
[2–3 line bullets per major concept — definition + one key property]

## 📐 Essential Formulas
[LaTeX display math for every important formula — name it, show it, add a one-line note]

## ⚠️ Watch Out (Common Mistakes)
[Based on the student's doubts — reframe each doubt as a trap to avoid]

## 📌 Your Highlights
[Bullet list of the key phrases/sentences the student highlighted — exact text]

## 🎯 Weak Areas to Revise
[Concepts with status=struggling or partial — brief note on what to focus on]

## 💡 Exam Tips
[3–5 sharp, actionable tips based on the material and student's level]

Rules:
- Use LaTeX: inline $...$ and display $$...$$ on its own line
- Keep each section tight — never more than 8 bullets
- Do NOT reproduce the full notes — distil them
- Output ONLY the Markdown — no preamble, no "Here is your cheatsheet:"
"""

_SHORT_NOTES_USER = """\
STUDENT PROFILE:
- Notebook: {notebook_name}
- Course: {course}
- Proficiency level: {proficiency}
- Concept mastery: {mastery_summary}

PERSONALISATION CONTEXT (adapt depth/style/pace to this learner):
{student_profile_context}

CONCEPTS WITH STATUS:
{concept_statuses}

DOUBTS THE STUDENT RAISED (address these as traps/tips):
{doubts_text}

STUDENT HIGHLIGHTS (include these verbatim in the Highlights section):
{highlights_text}

FULL NOTES (distil these — do not reproduce verbatim):
{notes_truncated}

Generate the cheatsheet now.
"""


@router.get("/api/notebooks/{nb_id}/short-notes")
async def get_short_notes(
    nb_id: str,
    authorization: Optional[str] = Header(None),
):
    """Return persisted AI cheatsheet for a notebook, if available."""
    from agents.notebook_store import get_short_note, has_previous_short_note_version

    user = get_current_user(authorization)
    _require_notebook_owner(nb_id, user)

    saved = get_short_note(nb_id)
    current_text = (saved or {}).get("content", "")
    has_previous = has_previous_short_note_version(nb_id, current_text)
    if not saved or not (saved.get("content") or "").strip():
        return JSONResponse({"exists": False, "content": "", "has_previous": has_previous})

    return JSONResponse(
        {
            "exists": True,
            "content": saved.get("content", ""),
            "updated_at": saved.get("updated_at"),
            "has_previous": has_previous,
        }
    )


@router.post("/api/notebooks/{nb_id}/short-notes/undo")
async def undo_short_notes(
    nb_id: str,
    authorization: Optional[str] = Header(None),
):
    """Return previous short-note version (if any) without changing saved latest."""
    from agents.notebook_store import (
        get_short_note,
        get_previous_short_note_version,
        has_previous_short_note_version,
    )

    user = get_current_user(authorization)
    _require_notebook_owner(nb_id, user)

    current = get_short_note(nb_id)
    current_text = (current or {}).get("content", "")
    prev = get_previous_short_note_version(nb_id, current_text)
    if not prev or not (prev.get("content") or "").strip():
        raise HTTPException(404, "No previous short notes found.")

    prev_text = prev.get("content", "")

    if not (current_text or "").strip():
        raise HTTPException(404, "No current short notes found.")

    # Non-destructive undo preview: latest stays persisted; UI can redo by switching
    # back to the saved latest already loaded from GET.
    return JSONResponse(
        {
            "exists": True,
            "content": prev_text,
            "updated_at": prev.get("created_at"),
            "has_previous": has_previous_short_note_version(nb_id, current_text),
            "mode": "previous",
        }
    )


@router.post("/api/notebooks/{nb_id}/short-notes")
async def generate_short_notes(
    nb_id: str,
    authorization: Optional[str] = Header(None),
):
    """Generate AI cheatsheet for a notebook. Returns SSE stream."""
    from agents.notebook_store import (
        get_notebook,
        get_doubts,
        get_annotations,
        get_short_note,
        save_short_note,
        create_short_note_version,
        has_previous_short_note_version,
    )
    from agents.mastery_store import get_db

    user = get_current_user(authorization)
    _require_notebook_owner(nb_id, user)
    _check_llm_rate_limit(user["id"])

    nb = get_notebook(nb_id)
    if not nb:
        raise HTTPException(404, "Notebook not found.")

    note    = nb.get("note", "") or ""
    prof    = nb.get("proficiency", "Practitioner")
    name    = nb.get("name", "Notebook")
    course  = nb.get("course", "")

    # Pull student profile context to personalise quick review style/depth.
    student_profile_context = ""
    try:
        import asyncio as _aio_sn
        from agents.behaviour_store import get_personalisation_context as _gpctx_sn
        student_profile_context = await _aio_sn.to_thread(_gpctx_sn, user["id"])
    except Exception:
        student_profile_context = ""

    # ── Mastery graph from Cosmos DB / SQLite ──────────────────────────────
    graph_data = get_db(user["id"])
    nodes = graph_data.get("nodes", [])
    # Also merge nodes from the notebook's own graph (more current)
    nb_nodes = (nb.get("graph") or {}).get("nodes", [])
    if nb_nodes:
        nodes = nb_nodes

    mastered   = [n for n in nodes if n.get("status") == "mastered"]
    partial    = [n for n in nodes if n.get("status") == "partial"]
    struggling = [n for n in nodes if n.get("status") == "struggling"]

    mastery_summary = (
        f"{len(mastered)} mastered, {len(partial)} partial, "
        f"{len(struggling)} struggling out of {len(nodes)} total"
    )
    concept_statuses = "\n".join(
        f"- {n.get('full_label') or n.get('label','?')}: {n.get('status','?')}"
        for n in nodes[:40]
    ) or "(no concept data)"

    # ── Doubts ────────────────────────────────────────────────────────────
    doubts   = get_doubts(nb_id)
    doubts_text = "\n".join(
        f"- Page {d.get('pageIdx',0)+1}: \"{d.get('doubt','')}\" "
        f"→ {d.get('insight','')[:120]}"
        for d in doubts[:20]
    ) or "(no doubts recorded)"

    # ── Highlights ────────────────────────────────────────────────────────
    anns = get_annotations(nb_id)
    highlights = [
        a.get("data", {}).get("text", "").strip()
        for a in anns
        if a.get("type") == "highlight" and a.get("data", {}).get("text", "").strip()
    ]
    highlights_text = "\n".join(f"- \"{h}\"" for h in highlights[:30]) or "(no highlights)"

    # ── Notes (truncate to fit context) ───────────────────────────────────
    notes_truncated = note[:12000] + ("…[truncated]" if len(note) > 12000 else "")

    user_prompt = _SHORT_NOTES_USER.format(
        notebook_name=name,
        course=course or "General",
        proficiency=prof,
        mastery_summary=mastery_summary,
        student_profile_context=(student_profile_context or "(no additional learner profile context)"),
        concept_statuses=concept_statuses,
        doubts_text=doubts_text,
        highlights_text=highlights_text,
        notes_truncated=notes_truncated,
    )

    messages = [
        {"role": "system", "content": _SHORT_NOTES_SYSTEM},
        {"role": "user",   "content": user_prompt},
    ]

    # ── Stream the cheatsheet ─────────────────────────────────────────────
    async def _stream():
        import json as _j
        raw = None
        if deps._is_azure_available():
            try:
                raw = await deps._azure_chat(messages, max_tokens=3000)
            except Exception as e:
                logger.warning("short-notes Azure failed: %s", e)
        if not raw and deps._is_groq_available():
            try:
                raw = await deps._groq_chat(messages, max_tokens=3000)
            except Exception as e:
                logger.warning("short-notes Groq failed: %s", e)
        if not raw:
            raw = f"# ⚡ Quick Review: {name}\n\n*Could not reach AI — try again.*"

        try:
            existing = get_short_note(nb_id)
            existing_text = (existing or {}).get("content", "")
            if (existing_text or "").strip() and existing_text.strip() != (raw or "").strip():
                create_short_note_version(nb_id, existing_text)
            save_short_note(nb_id, raw)
        except Exception as e:
            logger.warning("short-notes save failed for %s: %s", nb_id, e)

        _record_llm_call(user["id"], "azure" if deps._is_azure_available() else "groq", est_tokens=3000)

        yield f"data: {_j.dumps({'content': raw, 'done': True, 'has_previous': has_previous_short_note_version(nb_id, raw)})}\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")
