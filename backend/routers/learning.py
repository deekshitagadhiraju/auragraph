"""routers/learning.py — doubt, mutate, regenerate, examine, sniper-exam, concept-practice."""
from __future__ import annotations

import json as _json
import logging
import os
import re
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query

import deps
from deps import (
    get_current_user, _require_notebook_owner,
    _check_llm_rate_limit, _record_llm_call,
    _format_chunks_for_prompt, _note_to_pages,
)
from schemas import (
    DoubtRequest, DoubtResponse,
    MutationRequest, MutationResponse,
    RegenerateSectionRequest, RegenerateSectionResponse,
    SniperExamRequest, SniperExamResponse,
    GeneralExamRequest, GeneralExamResponse,
    ExaminerRequest, ExaminerResponse,
    ConceptPracticeRequest, ConceptPracticeResponse,
)

logger = logging.getLogger("auragraph")
router = APIRouter(tags=["learning"])


# ── Robust LLM JSON parser (handles LaTeX backslashes) ────────────────────────

def _parse_llm_json(text: str):
    """Parse JSON from LLM output, fixing LaTeX backslash issues.

    LLMs often produce unescaped LaTeX like \\theta which overlaps with JSON
    escape sequences (\\t = tab).  Two-pass: try raw first, then fix backslashes.
    """
    # Pass 1: direct parse
    try:
        return _json.loads(text)
    except _json.JSONDecodeError:
        pass
    # Pass 2: protect already-valid escapes, then double remaining backslashes
    fixed = text
    fixed = fixed.replace('\\\\', '\x00DBL\x00')    # protect \\
    fixed = fixed.replace('\\"',  '\x00QT\x00')      # protect \"
    fixed = fixed.replace('\\',   '\\\\')             # double all remaining
    fixed = fixed.replace('\x00DBL\x00', '\\\\')     # restore \\
    fixed = fixed.replace('\x00QT\x00',  '\\"')      # restore \"
    try:
        return _json.loads(fixed)
    except _json.JSONDecodeError as exc:
        logger.warning("LLM JSON parse failed after backslash fix: %s", exc)
        return None


def _question_signature(text: str) -> str:
    """Normalize question text for duplicate detection across attempts."""
    s = (text or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return s


def _extract_prior_question_texts(runs: list, max_items: int = 120) -> list[str]:
    """Collect question strings from stored quiz runs (newest-first list)."""
    out: list[str] = []
    for run in runs or []:
        for q in (run.get("questions") or []):
            txt = (q or {}).get("question") if isinstance(q, dict) else ""
            if isinstance(txt, str) and txt.strip():
                out.append(txt.strip())
                if len(out) >= max_items:
                    return out
    return out


def _filter_unique_questions(questions: list, disallowed_signatures: set[str], target_count: int) -> list:
    """Remove duplicates against history and within the same generated quiz."""
    accepted: list = []
    seen = set(disallowed_signatures or set())
    for q in questions or []:
        q_text = ""
        if isinstance(q, dict):
            q_text = str(q.get("question") or "")
        sig = _question_signature(q_text)
        if not sig or sig in seen:
            continue
        seen.add(sig)
        accepted.append(q)
        if len(accepted) >= max(1, int(target_count or 1)):
            break
    return accepted


def _build_repeat_guard_block(prior_questions: list[str], max_items: int = 40) -> str:
    """Prompt block listing prior questions to avoid repeating."""
    if not prior_questions:
        return ""
    lines = []
    for q in prior_questions[:max_items]:
        one_line = re.sub(r"\s+", " ", q).strip()
        if one_line:
            lines.append(f"- {one_line[:220]}")
    if not lines:
        return ""
    return (
        "\n\nIMPORTANT: Avoid repeating or paraphrasing any previously asked question from this notebook. "
        "Do not generate questions that are substantially similar to these:\n"
        + "\n".join(lines)
    )


def _extract_questions_from_raw(raw: str) -> list:
    """Parse model text into question-array JSON safely."""
    if not raw:
        return []
    try:
        clean = re.sub(r"^```[a-z]*\n?", "", raw.strip())
        clean = re.sub(r"\n?```$", "", clean.strip())
        if not clean.lstrip().startswith('['):
            m = re.search(r'\[[\s\S]+\]', clean)
            if m:
                clean = m.group(0)
        parsed = _parse_llm_json(clean)
        if isinstance(parsed, list):
            return parsed
    except Exception as e:
        logger.warning("Exam JSON parse failed: %s", e)
    return []


def _top_up_unique_questions(
    existing: list,
    concepts: list[str],
    target_count: int,
    prior_signatures: set[str],
    mode: str,
) -> list:
    """Ensure target count by generating deterministic non-duplicate backup MCQs."""
    out = list(existing or [])
    needed = max(1, int(target_count or 1))
    if len(out) >= needed:
        return out[:needed]

    seen = set(prior_signatures or set())
    for q in out:
        if isinstance(q, dict):
            sig = _question_signature(str(q.get("question") or ""))
            if sig:
                seen.add(sig)

    pool = [c for c in (concepts or []) if isinstance(c, str) and c.strip()]
    if not pool:
        pool = ["the topic"]

    stems = [
        "Which option best defines {c}?",
        "For {c}, which statement is most accurate?",
        "Which condition is essential when reasoning about {c}?",
        "What is the clearest conceptual meaning of {c}?",
        "Which explanation correctly captures {c}?",
        "Which statement about {c} is correct in this notebook context?",
    ]

    idx = 0
    while len(out) < needed and idx < (needed * 20):
        concept = pool[idx % len(pool)]
        stem = stems[(idx // max(1, len(pool))) % len(stems)]
        q_text = stem.format(c=concept)
        sig = _question_signature(q_text)
        idx += 1
        if not sig or sig in seen:
            continue
        seen.add(sig)
        out.append({
            "question": q_text,
            "options": {
                "A": f"A correct conceptual statement about {concept}.",
                "B": f"A common misconception about {concept}.",
                "C": f"An incomplete interpretation of {concept}.",
                "D": f"An unrelated claim that does not define {concept}.",
            },
            "correct": "A",
            "explanation": "Auto-generated backup question to maintain unique quiz coverage.",
            "concept": concept,
            "difficulty": "medium" if mode == "general" else "targeted",
        })

    return out[:needed]


# ── Doubt answering ────────────────────────────────────────────────────────────

@router.post("/api/doubt", response_model=DoubtResponse)
async def answer_doubt(
    req: DoubtRequest,
    authorization: Optional[str] = Header(None),
):
    from agents.knowledge_store import retrieve_relevant_chunks, get_note_page
    from agents.verifier_agent import parse_verification_response
    from agents.content_safety import check_output, check_input, sanitise_input
    from agents.latex_utils import fix_latex_delimiters
    from agents.behaviour_store import get_personalisation_context, track_doubt

    user = get_current_user(authorization)
    _check_llm_rate_limit(user["id"])
    _require_notebook_owner(req.notebook_id, user)

    # ── Screen student input before touching any LLM ──────────────────────────
    _input_safe, _input_cat = await check_input(req.doubt)
    if not _input_safe:
        from fastapi import HTTPException
        raise HTTPException(400, f"Your question contains content that cannot be processed ({_input_cat}). Please rephrase.")
    # Strip profanity — preserve academic intent, never echo curse words
    doubt_clean, _was_sanitised = sanitise_input(req.doubt)
    if _was_sanitised:
        logger.info("Input sanitised for user %s", user["id"])

    # Track doubt + get personalisation context concurrently in thread pool
    # (both are sync I/O — run off the event loop to avoid blocking)
    import asyncio as _aio
    def _track_and_ctx():
        try:
            track_doubt(
                user_id=user["id"],
                notebook_id=req.notebook_id,
                topic=" ".join(doubt_clean.split()[:6]),
                question=doubt_clean,
                page_idx=getattr(req, "page_idx", 0) or 0,
            )
        except Exception:
            pass
        try:
            return get_personalisation_context(user["id"])
        except Exception:
            return ""

    student_ctx = await _aio.to_thread(_track_and_ctx)

    slide_hits    = retrieve_relevant_chunks(req.notebook_id, doubt_clean, top_k=6, source_filter="slides")
    textbook_hits = retrieve_relevant_chunks(req.notebook_id, doubt_clean, top_k=6, source_filter="textbook")
    slide_ctx    = _format_chunks_for_prompt(slide_hits,    8_000)
    textbook_ctx = _format_chunks_for_prompt(textbook_hits, 8_000)
    note_page    = get_note_page(req.notebook_id, req.page_idx) or ""

    raw_text: str | None = None
    source = "local"

    if deps._is_azure_available():
        try:
            raw_text = str(await deps.fusion_agent.answer_doubt(
                doubt=doubt_clean, slide_context=slide_ctx,
                textbook_context=textbook_ctx, note_page=note_page,
                student_context=student_ctx,
            ))
            source = "azure"
        except Exception as e:
            logger.warning("Azure doubt failed: %s", e)

    if raw_text is None and deps._is_groq_available():
        try:
            raw_text = await deps._groq_doubt(doubt_clean, slide_ctx, textbook_ctx, note_page, student_context=student_ctx)
            source   = "groq"
        except Exception as e:
            logger.warning("Groq doubt failed: %s", e)

    if raw_text is not None:
        vr = parse_verification_response(raw_text)
        _safe, _cat = await check_output(vr.answer)
        if not _safe:
            logger.warning("Content Safety blocked doubt answer: category=%s", _cat)
            _record_llm_call(user["id"], source, est_tokens=1500)
            return DoubtResponse(
                answer="I'm unable to provide a response to this question as the generated answer was flagged by our content filter. Please try rephrasing your question.",
                source=source,
            )
        from agents.content_safety import strip_error_exposure_language
        clean_answer = strip_error_exposure_language(fix_latex_delimiters(vr.answer or ""))
        _record_llm_call(user["id"], source, est_tokens=1500)
        return DoubtResponse(
            answer=clean_answer,
            source=source,
            verification_status=vr.verification_status,
            correction=fix_latex_delimiters(vr.correction),
            footnote=vr.footnote,
        )

    from agents.local_mutation import _diagnose_gap, _build_analogy_hint
    gap     = _diagnose_gap(req.doubt)
    analogy = _build_analogy_hint(req.doubt)
    answer  = f"**{gap}**\n\n{analogy}"
    if note_page:
        answer += f"\n\n*From your notes:* {note_page[:300]}…"
    return DoubtResponse(answer=fix_latex_delimiters(answer), source="local")


# ── Mutation ───────────────────────────────────────────────────────────────────

@router.post("/api/mutate", response_model=MutationResponse)
async def mutate_note(
    req: MutationRequest,
    authorization: Optional[str] = Header(None),
):
    from agents.knowledge_store import retrieve_relevant_chunks, get_note_page, update_note_page, get_all_note_pages
    from agents.notebook_store import update_notebook_note
    from agents.mastery_store import update_node_status, increment_mutation_count
    from agents.content_safety import check_output, check_input, sanitise_input
    from agents.local_mutation import local_mutate
    from agents.concept_extractor import extract_concepts
    from agents.latex_utils import fix_latex_delimiters
    from pipeline.note_generator import _fix_tables

    user = get_current_user(authorization)
    _check_llm_rate_limit(user["id"])
    _require_notebook_owner(req.notebook_id, user)
    _username = user["id"]

    # Screen + sanitise student input before mutation
    _in_safe_mut, _in_cat_mut = await check_input(req.doubt or "")
    if not _in_safe_mut:
        raise HTTPException(400, f"Your doubt contains content that cannot be processed ({_in_cat_mut}).")
    _doubt_clean_mut, _ = sanitise_input(req.doubt or "")

    # Track mutation doubt in background (non-blocking)
    import asyncio as _aio2
    async def _bg_track_mut():
        try:
            from agents.behaviour_store import track_doubt as _td
            await _aio2.to_thread(_td, _username, req.notebook_id,
                " ".join(_doubt_clean_mut.split()[:6]), _doubt_clean_mut,
                getattr(req, "page_idx", 0) or 0)
        except Exception:
            pass
    _aio2.ensure_future(_bg_track_mut())

    note_page = get_note_page(req.notebook_id, req.page_idx)
    if note_page is None:
        note_page = req.original_paragraph or ""

    query         = _doubt_clean_mut + " " + note_page[:200]
    slide_hits    = retrieve_relevant_chunks(req.notebook_id, query, top_k=6, source_filter="slides")
    textbook_hits = retrieve_relevant_chunks(req.notebook_id, query, top_k=6, source_filter="textbook")
    slide_ctx     = _format_chunks_for_prompt(slide_hits,    8_000)
    textbook_ctx  = _format_chunks_for_prompt(textbook_hits, 8_000)

    # Pull student profile context for prompt personalisation.
    _mut_student_ctx = ""
    try:
        from agents.behaviour_store import get_personalisation_context as _gpctx_mut
        _mut_student_ctx = await _aio2.to_thread(_gpctx_mut, _username)
    except Exception:
        _mut_student_ctx = ""

    mutated, gap, answer, llm_source = await deps._llm_mutate(
        note_page,
        _doubt_clean_mut,
        slide_ctx,
        textbook_ctx,
        student_context=_mut_student_ctx,
    )

    if mutated is None:
        mutated, gap = local_mutate(note_page, _doubt_clean_mut)
        llm_source   = "local"
        answer       = ""

    can_mutate = llm_source in ("azure", "groq")
    mutated    = fix_latex_delimiters(_fix_tables(mutated))

    # ── ADDITIVE-ONLY GUARD ──────────────────────────────────────────────────
    # If the LLM shrank the page (deleted content), reject the rewrite and
    # instead prepend the new additions above the original content.
    if can_mutate and len(mutated.strip()) < len(note_page.strip()):
        logger.warning(
            "Mutation shrank page from %d → %d chars — falling back to additive prepend",
            len(note_page), len(mutated),
        )
        # Extract the heading from the original to avoid duplication
        import re as _re
        heading_match = _re.match(r'^(##\s+[^\n]+)\n', note_page.strip())
        heading = heading_match.group(1) if heading_match else None
        # Build the addition: intuition block from the gap + answer
        addition_parts = []
        if gap and gap != "Student required additional clarification.":
            addition_parts.append(f"> 💡 **Intuition (re: \"{_doubt_clean_mut.strip()}\"):** {gap}")
        if answer:
            addition_parts.append(answer)
        addition = "\n\n".join(addition_parts) if addition_parts else (
            f"> 💡 **Clarification:** See below for the original content addressing: \"{_doubt_clean_mut.strip()}\""
        )
        if heading:
            body = note_page.strip()[len(heading):].strip()
            mutated = f"{heading}\n\n{addition}\n\n{body}"
        else:
            mutated = f"{addition}\n\n{note_page.strip()}"

    if can_mutate and req.notebook_id:
        try:
            updated = update_note_page(req.notebook_id, req.page_idx, mutated)
            logger.info(
                "mutate_persist: notebook_id=%s page_idx=%s updated=%s mutated_len=%d",
                req.notebook_id,
                req.page_idx,
                bool(updated),
                len((mutated or "").strip()),
            )
            if updated:
                full_note = "\n\n".join(get_all_note_pages(req.notebook_id))
                update_notebook_note(req.notebook_id, full_note)
        except Exception as e:
            logger.warning("Page update failed: %s", e)

    from agents.content_safety import check_output as _cs_out
    _safe, _cat = await _cs_out(mutated)
    if not _safe:
        logger.warning("Content Safety blocked mutation: category=%s", _cat)
        return MutationResponse(
            mutated_paragraph=req.original_paragraph or "",
            concept_gap="Content moderation: the rewritten note was blocked. The original is preserved.",
            can_mutate=False,
        )

    if req.notebook_id and mutated:
        try:
            graph = extract_concepts(mutated)
            if graph.get("nodes"):
                top_concept = graph["nodes"][0]["label"]
                update_node_status(top_concept, "partial", _username)
                increment_mutation_count(top_concept, _username)
        except Exception:
            pass

    if can_mutate:
        _record_llm_call(user["id"], llm_source, est_tokens=3000)

    from agents.content_safety import strip_error_exposure_language
    safe_mutated = strip_error_exposure_language(mutated or "")
    safe_answer  = strip_error_exposure_language(fix_latex_delimiters(answer)) if answer else (gap or "")
    # Final guard: never return an empty mutated_paragraph — fall back to original
    if not safe_mutated.strip():
        safe_mutated = req.original_paragraph or note_page or ""
        logger.warning("mutation: safe_mutated was empty after processing — falling back to original page")

    return MutationResponse(
        mutated_paragraph=safe_mutated,
        concept_gap=gap or "Student required additional clarification.",
        answer=safe_answer,
        page_idx=req.page_idx,
        source=llm_source,
        can_mutate=can_mutate,
    )


# ── Regenerate section ─────────────────────────────────────────────────────────

@router.post("/api/regenerate-section", response_model=RegenerateSectionResponse)
async def regenerate_section(
    req: RegenerateSectionRequest,
    authorization: Optional[str] = Header(None),
):
    from agents.knowledge_store import retrieve_relevant_chunks, get_note_page, update_note_page, get_all_note_pages
    from agents.notebook_store import update_notebook_note
    from agents.latex_utils import fix_latex_delimiters
    from pipeline.note_generator import _fix_tables

    user              = get_current_user(authorization)
    _check_llm_rate_limit(user["id"])          # FIX: was missing — allowed unlimited API calls
    nb                = _require_notebook_owner(req.notebook_id, user)
    current_page_text = get_note_page(req.notebook_id, req.page_idx) or ""

    if not current_page_text and nb.get("note"):
        note_pages        = re.split(r'(?m)^(?=## )', nb["note"])
        note_pages        = [p.strip() for p in note_pages if p.strip()]
        if req.page_idx < len(note_pages):
            current_page_text = note_pages[req.page_idx]

    if not current_page_text:
        raise HTTPException(404, "Page not found in notebook")

    heading_match = re.match(r'^#{1,3}\s+(.+)', current_page_text)
    topic         = heading_match.group(1) if heading_match else current_page_text[:80]

    slide_hits    = retrieve_relevant_chunks(req.notebook_id, topic, top_k=8, source_filter="slides")
    textbook_hits = retrieve_relevant_chunks(req.notebook_id, topic, top_k=8, source_filter="textbook")
    slide_ctx     = _format_chunks_for_prompt(slide_hits,    10_000)
    textbook_ctx  = _format_chunks_for_prompt(textbook_hits, 10_000)

    # Personalisation context (non-blocking thread)
    import asyncio as _aio_r
    try:
        from agents.behaviour_store import get_personalisation_context as _gpctx
        _regen_student_ctx = await _aio_r.to_thread(_gpctx, user["id"])
    except Exception:
        _regen_student_ctx = ""

    custom_direction = (
        f"\nSTUDENT DIRECTION: {req.custom_prompt.strip()}\n"
        f"(Honour the student's direction above when writing this section.)\n"
        if req.custom_prompt and req.custom_prompt.strip() else ""
    )
    regen_prompt = (
        f"You are AuraGraph's note-generation engine. Re-write the following study note section "
        f"**from scratch**, using only the source material below.\n\n"
        f"TOPIC: {topic}\nPROFICIENCY LEVEL: {req.proficiency}\n"
        f"{custom_direction}\n"
        f"SOURCE MATERIAL:\n--- SLIDES ---\n{slide_ctx}\n\n--- TEXTBOOK ---\n{textbook_ctx}\n\n"
        f"INSTRUCTIONS:\n"
        f"- Write a single cohesive section starting with \"## {topic}\"\n"
        f"- Use LaTeX math ($...$ for inline, $$...$$ for display)\n"
        f"- Include key formulas, definitions, and intuition calibrated to {req.proficiency} level\n"
        f"- Do NOT copy the old note — write a fresh, improved version\n"
        f"- Output ONLY the markdown note section (no preamble)\n\n"
        + (_regen_student_ctx + "\n" if _regen_student_ctx else "")
    )

    llm_source  = "local"
    new_section = ""

    if deps._is_azure_available():
        try:
            new_section = await deps._azure_chat([{"role": "user", "content": regen_prompt}], max_tokens=3000)
            llm_source  = "azure"
        except Exception as e:
            logger.warning("Azure regenerate failed: %s", e)

    if not new_section and deps._is_groq_available():
        try:
            new_section = await deps._groq_chat([{"role": "user", "content": regen_prompt}], max_tokens=3000)
            llm_source  = "groq"
        except Exception as e:
            logger.warning("Groq regenerate failed: %s", e)

    if llm_source in ("azure", "groq"):
        _record_llm_call(user["id"], llm_source, est_tokens=3000)

    if not new_section:
        new_section = current_page_text + "\n\n> *(Regeneration unavailable — AI offline. Original section kept.)*"
        llm_source  = "local"
    else:
        new_section = fix_latex_delimiters(_fix_tables(new_section))
        try:
            update_note_page(req.notebook_id, req.page_idx, new_section)
            full_note = "\n\n".join(get_all_note_pages(req.notebook_id))
            update_notebook_note(req.notebook_id, full_note)
        except Exception as e:
            logger.warning("Failed to persist regenerated section: %s", e)

    return RegenerateSectionResponse(new_section=new_section, page_idx=req.page_idx, source=llm_source)


# ── Sniper exam ────────────────────────────────────────────────────────────────

@router.post("/api/sniper-exam", response_model=SniperExamResponse)
async def sniper_exam(
    req: SniperExamRequest,
    authorization: Optional[str] = Header(None),
):
    from agents.knowledge_store import build_quiz_context
    from agents.examiner_agent import SNIPER_EXAM_PROMPT
    from agents.notebook_store import list_quiz_runs

    user = get_current_user(authorization)
    _check_llm_rate_limit(user["id"])     # FIX: was missing
    if req.notebook_id:
        _require_notebook_owner(req.notebook_id, user)

    # Use weak_concepts from frontend (authoritative source of graph state)
    struggling = (req.weak_concepts or [])[:5]

    if not struggling:
        return SniperExamResponse(questions=[], concepts_tested=[], quiz_id=None)

    partial: list[str] = []

    concepts_tested = (
        [{"label": l, "status": "struggling"} for l in struggling] +
        [{"label": l, "status": "partial"}    for l in partial]
    )

    prior_questions: list[str] = []
    prior_signatures: set[str] = set()
    student_profile_context = ""
    if req.notebook_id:
        try:
            previous_runs = list_quiz_runs(req.notebook_id, test_type="sniper", limit=300)
            prior_questions = _extract_prior_question_texts(previous_runs, max_items=200)
            prior_signatures = {_question_signature(q) for q in prior_questions if _question_signature(q)}
        except Exception as e:
            logger.warning("Could not load prior sniper quizzes for dedupe: %s", e)

    try:
        import asyncio as _aio_qs
        from agents.behaviour_store import get_personalisation_context as _gpctx_qs
        student_profile_context = await _aio_qs.to_thread(_gpctx_qs, user["id"])
    except Exception:
        student_profile_context = ""

    nb_ctx = build_quiz_context(req.notebook_id, " ".join(struggling + partial)) if req.notebook_id else ""

    candidate_target = 12

    def _build_prompt():
        return (
            SNIPER_EXAM_PROMPT
            .replace("{{$struggling_concepts}}", ", ".join(struggling) or "None")
            .replace("{{$partial_concepts}}",    ", ".join(partial)    or "None")
            .replace("{{$notebook_context}}",    nb_ctx or "(no course context available)")
                        + (f"\n\n{student_profile_context}\n" if student_profile_context else "")
            + f"\n\nReturn at least {candidate_target} unique MCQs in valid JSON array format."
              " We will select the best non-repeating subset."
            + _build_repeat_guard_block(prior_questions)
        )

    raw = ""
    logger.info("sniper-exam: struggling=%s partial=%s azure=%s groq=%s",
                struggling, partial, deps._is_azure_available(), deps._is_groq_available())
    if deps._is_azure_available():
        try:
            raw = await deps._azure_chat([{"role": "user", "content": _build_prompt()}], max_tokens=6500)
            logger.info("Azure sniper exam OK, len=%d", len(raw) if raw else 0)
        except Exception as e:
            logger.warning("Azure sniper exam failed: %s — %s", type(e).__name__, e)
    if not raw and deps._is_groq_available():
        try:
            raw = await deps._groq_chat([{"role": "user", "content": _build_prompt()}], max_tokens=5000)
            logger.info("Groq sniper exam OK, len=%d", len(raw) if raw else 0)
        except Exception as e:
            logger.warning("Groq sniper exam failed: %s — %s", type(e).__name__, e)

    questions = _filter_unique_questions(_extract_questions_from_raw(raw), prior_signatures, target_count=5)

    # Quality-first top-up pass for missing questions. Default 1 pass.
    supplement_passes = max(0, int(os.environ.get("QUIZ_SUPPLEMENT_PASSES", "1") or "1"))
    if len(questions) < 5 and supplement_passes > 0 and (deps._is_azure_available() or deps._is_groq_available()):
        for _ in range(supplement_passes):
            if len(questions) >= 5:
                break
            missing = 5 - len(questions)
            avoid = prior_questions + [str((q or {}).get("question") or "") for q in questions if isinstance(q, dict)]
            supplement_prompt = (
                _build_prompt()
                + f"\n\nYou already generated some items. Now generate at least {missing + 5} ADDITIONAL high-quality MCQs."
                  " They must be unique, exam-level, and grounded in the provided notebook context."
                  " Do NOT repeat, paraphrase, or trivially reorder previously generated questions."
                + _build_repeat_guard_block(avoid, max_items=80)
            )
            sup_raw = ""
            if deps._is_azure_available():
                try:
                    sup_raw = await deps._azure_chat([{"role": "user", "content": supplement_prompt}], max_tokens=5500)
                except Exception as e:
                    logger.warning("Azure sniper supplement failed: %s", e)
            if not sup_raw and deps._is_groq_available():
                try:
                    sup_raw = await deps._groq_chat([{"role": "user", "content": supplement_prompt}], max_tokens=4500)
                except Exception as e:
                    logger.warning("Groq sniper supplement failed: %s", e)
            if not sup_raw:
                continue
            merged = list(questions) + _extract_questions_from_raw(sup_raw)
            questions = _filter_unique_questions(merged, prior_signatures, target_count=5)

    # Local template top-up only in true offline mode (both providers unavailable).
    if len(questions) < 5 and not (deps._is_azure_available() or deps._is_groq_available()):
        questions = _top_up_unique_questions(
            questions,
            concepts=(struggling + partial),
            target_count=5,
            prior_signatures=prior_signatures,
            mode="sniper",
        )

    return SniperExamResponse(questions=questions, concepts_tested=concepts_tested, quiz_id=None)

# ── General exam ────────────────────────────────────────────────────────────────────

@router.post("/api/general-exam", response_model=GeneralExamResponse)
async def general_exam(
    req: GeneralExamRequest,
    authorization: Optional[str] = Header(None),
):
    from agents.knowledge_store import build_quiz_context
    from agents.examiner_agent import GENERAL_EXAM_PROMPT
    from agents.notebook_store import list_quiz_runs

    user = get_current_user(authorization)
    _check_llm_rate_limit(user["id"])
    if req.notebook_id:
        _require_notebook_owner(req.notebook_id, user)

    all_concepts = (req.all_concepts or [])[:15]
    if not all_concepts:
        return GeneralExamResponse(questions=[], concepts_tested=[], quiz_id=None)

    concepts_tested = [{"label": l, "status": "all"} for l in all_concepts]

    prior_questions: list[str] = []
    prior_signatures: set[str] = set()
    student_profile_context = ""
    if req.notebook_id:
        try:
            previous_runs = list_quiz_runs(req.notebook_id, test_type="general", limit=300)
            prior_questions = _extract_prior_question_texts(previous_runs, max_items=300)
            prior_signatures = {_question_signature(q) for q in prior_questions if _question_signature(q)}
        except Exception as e:
            logger.warning("Could not load prior general quizzes for dedupe: %s", e)

    try:
        import asyncio as _aio_qg
        from agents.behaviour_store import get_personalisation_context as _gpctx_qg
        student_profile_context = await _aio_qg.to_thread(_gpctx_qg, user["id"])
    except Exception:
        student_profile_context = ""

    nb_ctx = build_quiz_context(req.notebook_id, " ".join(all_concepts)) if req.notebook_id else ""

    candidate_target = 20

    def _build_prompt():
        return (
            GENERAL_EXAM_PROMPT
            .replace("{{$all_concepts}}", ", ".join(all_concepts))
            .replace("{{$notebook_context}}", nb_ctx or "(no course context available)")
                        + (f"\n\n{student_profile_context}\n" if student_profile_context else "")
            + f"\n\nReturn at least {candidate_target} unique MCQs in valid JSON array format."
              " We will select the best non-repeating subset."
            + _build_repeat_guard_block(prior_questions)
        )

    raw = ""
    if deps._is_azure_available():
        try:
            raw = await deps._azure_chat([{"role": "user", "content": _build_prompt()}], max_tokens=9000)
        except Exception as e:
            logger.warning("Azure general exam failed: %s", e)
    if not raw and deps._is_groq_available():
        try:
            raw = await deps._groq_chat([{"role": "user", "content": _build_prompt()}], max_tokens=6500)
        except Exception as e:
            logger.warning("Groq general exam failed: %s", e)

    questions = _filter_unique_questions(_extract_questions_from_raw(raw), prior_signatures, target_count=10)

    # Quality-first top-up pass for missing questions. Default 1 pass.
    supplement_passes = max(0, int(os.environ.get("QUIZ_SUPPLEMENT_PASSES", "1") or "1"))
    if len(questions) < 10 and supplement_passes > 0 and (deps._is_azure_available() or deps._is_groq_available()):
        for _ in range(supplement_passes):
            if len(questions) >= 10:
                break
            missing = 10 - len(questions)
            avoid = prior_questions + [str((q or {}).get("question") or "") for q in questions if isinstance(q, dict)]
            supplement_prompt = (
                _build_prompt()
                + f"\n\nYou already generated some items. Now generate at least {missing + 8} ADDITIONAL high-quality MCQs."
                  " They must be unique, concept-balanced, and grounded in the provided notebook context."
                  " Do NOT repeat, paraphrase, or trivially reorder previously generated questions."
                + _build_repeat_guard_block(avoid, max_items=100)
            )
            sup_raw = ""
            if deps._is_azure_available():
                try:
                    sup_raw = await deps._azure_chat([{"role": "user", "content": supplement_prompt}], max_tokens=7500)
                except Exception as e:
                    logger.warning("Azure general supplement failed: %s", e)
            if not sup_raw and deps._is_groq_available():
                try:
                    sup_raw = await deps._groq_chat([{"role": "user", "content": supplement_prompt}], max_tokens=5500)
                except Exception as e:
                    logger.warning("Groq general supplement failed: %s", e)
            if not sup_raw:
                continue
            merged = list(questions) + _extract_questions_from_raw(sup_raw)
            questions = _filter_unique_questions(merged, prior_signatures, target_count=10)

    # Local template top-up only in true offline mode (both providers unavailable).
    if len(questions) < 10 and not (deps._is_azure_available() or deps._is_groq_available()):
        questions = _top_up_unique_questions(
            questions,
            concepts=all_concepts,
            target_count=10,
            prior_signatures=prior_signatures,
            mode="general",
        )

    return GeneralExamResponse(questions=questions, concepts_tested=concepts_tested, quiz_id=None)

# ── Examiner ───────────────────────────────────────────────────────────────────

@router.post("/api/examine", response_model=ExaminerResponse)
async def examine_concept(
    req: ExaminerRequest,
    authorization: Optional[str] = Header(None),
):
    from agents.knowledge_store import build_quiz_context
    from agents.local_examiner import local_examine
    from agents.latex_utils import fix_latex_delimiters
    from agents.examiner_agent import EXAMINER_PROMPT

    user = get_current_user(authorization)
    _check_llm_rate_limit(user["id"])     # FIX: was missing
    if req.notebook_id:
        _require_notebook_owner(req.notebook_id, user)

    ci     = (req.custom_instruction or "").strip()
    nb_ctx = build_quiz_context(req.notebook_id, req.concept_name) if req.notebook_id else ""

    if deps.examiner_agent and deps._is_azure_available():
        try:
            q = await deps.examiner_agent.examine(req.concept_name, notebook_context=nb_ctx, custom_instruction=ci)
            return ExaminerResponse(practice_questions=fix_latex_delimiters(q))
        except Exception as e:
            logger.warning("Azure examiner failed: %s", e)

    if deps._is_groq_available():
        try:
            ci_full = f"\n\nCUSTOM FOCUS (follow exactly): {ci}" if ci else ""
            prompt  = (
                EXAMINER_PROMPT
                .replace("{{$concept_name}}", req.concept_name)
                .replace("{{$notebook_context}}", nb_ctx or "(no course context available)")
                .replace("{{$custom_instruction}}", ci_full)
            )
            q = await deps._groq_chat([{"role": "user", "content": prompt}], max_tokens=4000)
            return ExaminerResponse(practice_questions=fix_latex_delimiters(q))
        except Exception as e:
            logger.warning("Groq examiner failed: %s", e)

    return ExaminerResponse(practice_questions=fix_latex_delimiters(local_examine(req.concept_name)))


# ── Concept practice ───────────────────────────────────────────────────────────

@router.post("/api/concept-practice", response_model=ConceptPracticeResponse)
async def concept_practice_endpoint(
    req: ConceptPracticeRequest,
    authorization: Optional[str] = Header(None),
):
    from agents.knowledge_store import build_quiz_context
    from agents.examiner_agent import CONCEPT_PRACTICE_PROMPT

    user  = get_current_user(authorization)
    _check_llm_rate_limit(user["id"])     # FIX: was missing
    if req.notebook_id:
        _require_notebook_owner(req.notebook_id, user)

    level  = req.level.lower().strip()
    if level not in ("struggling", "partial", "mastered"):
        level = "partial"
    ci     = (req.custom_instruction or "").strip()
    nb_ctx = build_quiz_context(req.notebook_id, req.concept_name) if req.notebook_id else ""

    ci_full = f"\n\nCUSTOM FOCUS (follow exactly): {ci}" if ci else ""

    def _build_prompt():
        return (
            CONCEPT_PRACTICE_PROMPT
            .replace("{{$concept_name}}", req.concept_name)
            .replace("{{$level}}",        level)
            .replace("{{$notebook_context}}", nb_ctx or "(no course context available)")
            .replace("{{$custom_instruction}}", ci_full)
        )

    raw: str | None = None
    logger.info("concept-practice: examiner=%s azure=%s groq=%s concept=%s level=%s",
                bool(deps.examiner_agent), deps._is_azure_available(),
                deps._is_groq_available(), req.concept_name, level)
    if deps.examiner_agent and deps._is_azure_available():
        try:
            raw = await deps.examiner_agent.concept_practice(
                req.concept_name, level, notebook_context=nb_ctx, custom_instruction=ci
            )
            logger.info("Azure concept-practice OK, len=%d", len(raw) if raw else 0)
        except Exception as e:
            logger.warning("Azure concept-practice failed: %s — %s", type(e).__name__, e)

    if raw is None and deps._is_groq_available():
        try:
            raw = await deps._groq_chat([{"role": "user", "content": _build_prompt()}], max_tokens=2000)
            logger.info("Groq concept-practice OK, len=%d", len(raw) if raw else 0)
        except Exception as e:
            logger.warning("Groq concept-practice failed: %s — %s", type(e).__name__, e)

    if raw:
        stripped = re.sub(r'^```(?:json)?\s*', '', raw.strip())
        stripped = re.sub(r'\s*```$', '', stripped.strip())
        if not stripped.lstrip().startswith('['):
            m = re.search(r'\[[\s\S]+\]', stripped)
            if m:
                stripped = m.group(0)
        parsed = _parse_llm_json(stripped)
        if isinstance(parsed, list) and parsed:
            return ConceptPracticeResponse(questions=parsed)

    return ConceptPracticeResponse(questions=[{
        "question": f"Which of the following best describes '{req.concept_name}'?",
        "options": {"A": "Option A", "B": "Option B", "C": "Option C", "D": "Option D"},
        "correct": "A",
        "explanation": "Backend offline — reconnect for AI-generated questions.",
    }])


# ── Behaviour tracking endpoints ──────────────────────────────────────────────

from pydantic import BaseModel as _BM

class TrackQuizAnswerRequest(_BM):
    notebook_id: str
    concept: str
    question: str
    correct: bool

class TrackHighlightRequest(_BM):
    notebook_id: str
    text: str
    page_idx: int = 0


class AuraStateRequest(_BM):
    xp: int = 0
    quizzesCompleted: int = 0
    correctAnswers: int = 0
    totalAnswers: int = 0
    doubtsAsked: int = 0
    highlightsAdded: int = 0
    activeTheme: str = "default"


class QuizScoreRequest(_BM):
    score: int
    total_questions: int
    correct_answers: Optional[int] = None


class QuizCompletionRequest(_BM):
    test_type: str
    questions: list
    concepts_tested: list
    responses: Optional[list] = None
    score: int
    total_questions: int
    correct_answers: Optional[int] = None


@router.get("/api/aura")
async def get_aura_state(
    authorization: Optional[str] = Header(None),
):
    """Returns the current user's Aura XP profile from server storage."""
    from agents.aura_store import get_aura
    user = get_current_user(authorization)
    return {"aura": get_aura(user["id"])}


@router.put("/api/aura")
async def save_aura_state(
    req: AuraStateRequest,
    authorization: Optional[str] = Header(None),
):
    """Upserts the current user's Aura XP profile to server storage."""
    from agents.aura_store import save_aura
    user = get_current_user(authorization)
    saved = save_aura(user["id"], req.model_dump())
    return {"ok": True, "aura": saved}


@router.post("/api/notebooks/{nb_id}/quizzes/{quiz_id}/score")
async def save_quiz_score(
    nb_id: str,
    quiz_id: str,
    req: QuizScoreRequest,
    authorization: Optional[str] = Header(None),
):
    """Persist final score for a generated quiz run."""
    from agents.notebook_store import complete_quiz_run

    user = get_current_user(authorization)
    _require_notebook_owner(nb_id, user)

    saved = complete_quiz_run(
        nb_id,
        quiz_id,
        score=req.score,
        total_questions=req.total_questions,
        correct_answers=req.correct_answers,
    )
    if not saved:
        raise HTTPException(404, "Quiz run not found")
    return {"ok": True, "quiz": saved}


@router.post("/api/notebooks/{nb_id}/quizzes/complete")
async def complete_quiz_run_endpoint(
    nb_id: str,
    req: QuizCompletionRequest,
    authorization: Optional[str] = Header(None),
):
    """Persist a completed quiz (questions + concepts + final score) in one shot."""
    from agents.notebook_store import create_completed_quiz_run

    user = get_current_user(authorization)
    _require_notebook_owner(nb_id, user)

    test_type = (req.test_type or "").strip().lower()
    if test_type not in ("general", "sniper"):
        raise HTTPException(400, "Invalid test_type. Use 'general' or 'sniper'.")

    saved = create_completed_quiz_run(
        nb_id,
        test_type,
        req.questions or [],
        req.concepts_tested or [],
        score=req.score,
        total_questions=req.total_questions,
        correct_answers=req.correct_answers,
        responses=req.responses or [],
    )
    if not saved:
        raise HTTPException(500, "Could not save quiz result")
    return {"ok": True, "quiz": saved}


@router.get("/api/notebooks/{nb_id}/quizzes")
async def get_notebook_quizzes(
    nb_id: str,
    test_type: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    authorization: Optional[str] = Header(None),
):
    """Return stored quiz runs for a notebook (general/sniper), newest first."""
    from agents.notebook_store import list_quiz_runs

    user = get_current_user(authorization)
    _require_notebook_owner(nb_id, user)

    runs = list_quiz_runs(nb_id, test_type=test_type, limit=limit)
    return {"runs": runs}


@router.post("/api/behaviour/track-quiz")
async def track_quiz_answer_endpoint(
    req: TrackQuizAnswerRequest,
    authorization: Optional[str] = Header(None),
):
    """Called from frontend after each quiz answer to track learning behaviour."""
    from agents.behaviour_store import track_quiz_answer
    user = get_current_user(authorization)
    try:
        track_quiz_answer(
            user_id=user["id"],
            notebook_id=req.notebook_id,
            concept=req.concept,
            question=req.question,
            correct=req.correct,
        )
    except Exception as e:
        logger.warning("track_quiz_answer failed: %s", e)
    return {"ok": True}


@router.post("/api/behaviour/track-highlight")
async def track_highlight_endpoint(
    req: TrackHighlightRequest,
    authorization: Optional[str] = Header(None),
):
    """Called from frontend when a highlight annotation is added."""
    from agents.behaviour_store import track_highlight
    user = get_current_user(authorization)
    try:
        track_highlight(
            user_id=user["id"],
            notebook_id=req.notebook_id,
            text=req.text,
            page_idx=req.page_idx,
        )
    except Exception as e:
        logger.warning("track_highlight failed: %s", e)
    return {"ok": True}


@router.get("/api/behaviour/profile")
async def get_behaviour_profile(
    authorization: Optional[str] = Header(None),
):
    """Returns the derived personalisation profile for the current user."""
    from agents.behaviour_store import get_profile
    user = get_current_user(authorization)
    try:
        profile = get_profile(user["id"])
        return {"profile": profile}
    except Exception as e:
        logger.warning("get_behaviour_profile failed: %s", e)
        return {"profile": {}}
