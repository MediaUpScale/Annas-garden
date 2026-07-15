# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config as app_config
from avatar_engine.knowledge.pdf_loader import corpus_to_prompt_context
from avatar_engine.providers.gemini_utils import generate_text_with_client_chain
from persona_dna import persona_context_block

_MAX_CHARS = 120_000


def build_imagine_subject_instruction() -> str:
    persona = persona_context_block()
    return dedent(
        f"""
        You are the editorial strategist for The Holistic Legacy.

        {persona}

        Task: From the excerpted Digital Product corpus (holistic / naturopathic guides), propose ONE concise
        content subject (2-7 words) that:
        - is strongly supported by the excerpts (themes, compounds, modalities actually mentioned),
        - fits Anna's biochemical, non-hype positioning,
        - would resonate on Instagram/Pinterest premium wellness audiences today.

        Output rules:
        - Return ONLY the subject line. No quotation marks. No preamble. One line only.
        """
    ).strip()


def imagine_subject(pdf_bundle: dict[str, str], *, model_id: str | None = None) -> str:
    """Ask Gemini to invent a topic from PDF excerpts + persona (live API call)."""
    key = app_config.GEMINI_API_KEY
    if not key:
        raise ValueError("GEMINI_API_KEY missing; cannot invent subject.")

    ctx = corpus_to_prompt_context(pdf_bundle)
    if len(ctx) > _MAX_CHARS:
        ctx = ctx[:_MAX_CHARS]

    instruction = build_imagine_subject_instruction()
    pref = model_id or app_config.GEMINI_RESEARCH_MODEL

    response = generate_text_with_client_chain(
        api_key=key,
        preferred_model=pref,
        contents=[instruction, "\n\nCORPUS BEGIN\n", ctx, "\nCORPUS END"],
    )

    text_attr = getattr(response, "text", None)
    line = text_attr() if callable(text_attr) else text_attr
    if not line:
        parts_out: list[str] = []
        for cand in getattr(response, "candidates", []) or []:
            content = getattr(cand, "content", None)
            if not content:
                continue
            for part in getattr(content, "parts", []) or []:
                t = getattr(part, "text", None)
                if t:
                    parts_out.append(t)
        line = "\n".join(parts_out)

    subject = (line or "").strip().splitlines()[0].strip().strip("\"'") if line else ""
    return subject or "Holistic vitality protocol"


def imagine_subject_instruction_preview() -> str:
    """Dry-run scaffold for Gemini subject imagination."""
    return build_imagine_subject_instruction()
