# -*- coding: utf-8 -*-
from __future__ import annotations

import random
import sys
import logging
from pathlib import Path
from textwrap import dedent

from anthropic import Anthropic
from google import genai

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config as app_config
from avatar_engine.knowledge.pdf_loader import corpus_to_prompt_context
from avatar_engine.providers.gemini_utils import (
    build_model_chain,
    chain_with_preferred_first,
    generate_content_with_model_fallback,
    get_active_model_id,
    make_gemini_client_with_fallback,
)
from persona_dna import (
    BATCH_DEFAULT_SIZE,
    BATCH_DELIMITER_OPEN,
    BATCH_ROTATION_PATTERN,
    BATCH_WORDS_PER_NARRATIVE,
    CTA_VOICE_INSTRUCTION,
    NARRATIVE_ANGLES,
    contextual_cta_keyword,
    persona_context_block,
)

logger = logging.getLogger(__name__)

_MAX_PROMPT_CHARS = 120_000

# ---------------------------------------------------------------------------
# Caption format: 60 % short (300-500 ch), 40 % long (500-800 ch)
# ---------------------------------------------------------------------------

_SHORT_FORM_CHANCE = 0.60

_VOICE_BASE = (
    "VOICE RULES (non-negotiable):\n"
    '- Style: "Dica de Dona de Casa" (Housewife Tip). Warm, personal, clinically grounded.\n'
    "- ABSOLUTELY NO headers, bold lists, bullet points, or fact-sheet labels.\n"
    "- No AI-sales words: Unlock / Dive / Elevate / Game-changer / Discover / Harness / Revolutionary.\n"
    "- Biochemically accurate, spoken like a trusted neighbour not a textbook.\n"
    "- Begin with a compelling conversational hook (never start with 'Did you know...').\n"
    "- End with a decisive CTA embedding the provided keyword.\n"
    "- Output ONLY the caption text -- no preamble, no labels, no markdown."
)

_SHORT_FORMAT = (
    "FORMAT: SHORT FORM -- one punchy paragraph, 350-450 characters.\n"
    "Focus on a single golden tip. Dense, specific, zero filler."
)

_LONG_FORMAT = (
    "FORMAT: LONG FORM -- 3-4 short paragraphs, 550-750 characters.\n"
    "Tell a micro-story or frame 'The Legacy' context before delivering the core tip.\n"
    "Each paragraph must carry its own weight -- no padding or repetition."
)


def _pick_format_rules() -> str:
    """Return voice + format block, randomly short (60 %) or long (40 %)."""
    fmt = _SHORT_FORMAT if random.random() < _SHORT_FORM_CHANCE else _LONG_FORMAT
    return f"{_VOICE_BASE}\n\n{fmt}"


# ---------------------------------------------------------------------------
# Variant helpers
# ---------------------------------------------------------------------------

def _variant_suffix(variation_index: int, total_variants: int) -> str:
    if total_variants <= 1:
        return ""
    return dedent(
        f"""
        Variation mandate: Creative variant #{variation_index + 1} of {total_variants}.
        Deliver a visibly different opening emphasis from other variants yet keep facts faithful to the FACT SHEET.
        """
    ).strip()


# ---------------------------------------------------------------------------
# Researcher prompt (kept intact -- produces structured fact sheet)
# ---------------------------------------------------------------------------

def build_gemini_researcher_instruction(topic: str, *, variation_notes: str = "") -> str:
    persona = persona_context_block()
    return dedent(
        f"""
        You are the Research Desk for `{topic}` at The Holistic Legacy.

        {persona}
        {variation_notes}

        Constraints:
        - Read ONLY what is verbatim in SOURCE FILE excerpts.
        - If the excerpts lack data, declare missing evidence explicitly rather than hallucinating.
        - Pull precise biochemical language (substrates, pathways, enzyme names) when quoted in sources.

        Produce a RAW FACT SHEET with these sections exactly:
        1. Verified Mechanisms (bullets quoting page hints / filenames).
        2. Safety Flags & Contraindications (explicit if absent).
        3. Protocol Notes From Guides (timing, dosing, cautions-if-stated-only).
        4. Monetization Spine: Identify the SINGLE best Payhip link / offer string present in excerpts.
           If ambiguous, enumerate candidate strings with rationale.
        5. Confidence Statement (High/Medium/Low) plus gaps.

        Keep tone clinical; this is downstream for an editor persona.
        """
    ).strip()


def build_batch_researcher_instruction(topic: str, num_variants: int) -> str:
    """
    Build a single-call prompt that asks Gemini to produce `num_variants`
    distinct research narratives in one response (the 'One-Call Rule').

    Each narrative is separated by the delimiter ===NARRATIVE_N=== so the
    response can be parsed into individual fact-sheets without extra API calls.
    """
    persona = persona_context_block()

    angle_lines = "\n".join(
        f"  - {a.get('code', '')}: {a.get('description', '')}"
        for a in NARRATIVE_ANGLES
    ) if NARRATIVE_ANGLES else (
        "  - SCIENTIFIC: Biochemical mechanism\n"
        "  - LEGACY: Ancestral wisdom story\n"
        "  - EXPOSE: Why this is suppressed"
    )

    rotation_note = BATCH_ROTATION_PATTERN or (
        "Rotate through SCIENTIFIC, LEGACY, EXPOSE angles in order. "
        "No two consecutive narratives may share the same angle."
    )

    example_delimiter = f"{BATCH_DELIMITER_OPEN}1==="

    return dedent(
        f"""
        You are the Research Desk for The Holistic Legacy. Your task is to generate
        {num_variants} DISTINCT research narratives about the topic: `{topic}`.

        {persona}

        BATCH RULES (non-negotiable):
        - Generate EXACTLY {num_variants} narratives, numbered 1 through {num_variants}.
        - Each narrative must be {BATCH_WORDS_PER_NARRATIVE} words.
        - Separate each narrative with EXACTLY this delimiter: {example_delimiter}
          (use the correct number for each, e.g. ===NARRATIVE_1===, ===NARRATIVE_2===, etc.)
        - Do NOT include any text before ===NARRATIVE_1=== or after the last narrative.

        NARRATIVE ANGLES -- rotate through these in order across the batch:
        {angle_lines}

        ROTATION RULE: {rotation_note}

        CONTENT RULES:
        - Pull facts ONLY from the PDF corpus below. Do not hallucinate.
        - Include precise biochemical language when quoted in sources.
        - Each narrative must have a unique opening sentence -- no repeated hooks.
        - Identify any Payhip link / offer present in excerpts; embed in narrative 1 only.
        - Never reference author names that are on the banned list from the persona block above.
        - Contrast natural mechanisms against expensive synthetic alternatives where relevant.

        OUTPUT FORMAT (start immediately with ===NARRATIVE_1===, nothing before it):
        ===NARRATIVE_1===
        [narrative content here]
        ===NARRATIVE_2===
        [narrative content here]
        ... continue through ===NARRATIVE_{num_variants}===
        """
    ).strip()


def _parse_batch_narratives(raw_response: str, num_variants: int) -> list[str]:
    """
    Split a batch research response into individual narrative strings.

    Expects delimiters in the form ===NARRATIVE_N=== produced by Gemini.
    Returns a list of `num_variants` strings; pads with empty strings if
    fewer narratives were returned than expected.
    """
    import re as _re
    parts = _re.split(r"===NARRATIVE_\d+===", raw_response)
    # parts[0] is text before the first delimiter (should be empty / preamble)
    narratives = [p.strip() for p in parts[1:] if p.strip()]
    # Pad to the requested count so callers can safely index
    while len(narratives) < num_variants:
        narratives.append("")
    return narratives[:num_variants]


# ---------------------------------------------------------------------------
# Humanizer prompts (Dica de Dona de Casa voice + variable length)
# ---------------------------------------------------------------------------

def build_claude_humanizer_system_prompt(topic_brand: str = "Anna") -> str:
    return f"You are {topic_brand}. {persona_context_block()}"


def build_claude_humanizer_user_prompt(
    topic: str,
    raw_fact_sheet: str,
    *,
    variation_index: int = 0,
    total_variants: int = 1,
    cta_keyword: str | None = None,
) -> str:
    kw = cta_keyword or contextual_cta_keyword(topic)
    format_rules = _pick_format_rules()
    suffix = _variant_suffix(variation_index, total_variants)
    cta_instruction = CTA_VOICE_INSTRUCTION or (
        f"Weave 'Comment {kw}' naturally into the caption as a personal DM invitation."
    )
    tail = f"\n\nTopic focus: `{topic}`\n\nFACT SHEET:\n```\n{raw_fact_sheet}\n```"
    body = (
        f"{format_rules}\n"
        f"CTA keyword for this caption: {kw}\n"
        f"CTA instruction: {cta_instruction}\n"
        "Include the Payhip URL verbatim if present in FACT SHEET; omit if absent."
    )
    return (body + f"\n\n{suffix}" + tail) if suffix else (body + tail)


def build_gemini_humanizer_instruction(
    topic: str,
    raw_fact_sheet: str,
    *,
    variation_index: int = 0,
    total_variants: int = 1,
    cta_keyword: str | None = None,
) -> str:
    persona = persona_context_block()
    kw = cta_keyword or contextual_cta_keyword(topic)
    format_rules = _pick_format_rules()
    suffix = _variant_suffix(variation_index, total_variants)
    cta_instruction = CTA_VOICE_INSTRUCTION or (
        f"Weave 'Comment {kw}' naturally into the caption as a personal DM invitation."
    )
    return dedent(
        f"""
        {persona}

        {format_rules}
        CTA keyword for this caption: {kw}
        CTA instruction: {cta_instruction}

        {suffix}

        Topic focus: `{topic}`

        FACT SHEET:
        ```
        {raw_fact_sheet}
        ```

        Include the Payhip URL verbatim if present in FACT SHEET. Omit if absent.
        Output ONLY the caption -- no labels, no markdown.
        """
    ).strip()


def humanizer_preview_with_placeholder(topic: str) -> tuple[str, str]:
    placeholder = "[DYNAMIC: RAW FACT SHEET FROM GEMINI RESEARCHER WOULD FOLLOW]"
    system = build_claude_humanizer_system_prompt("Anna")
    user = build_claude_humanizer_user_prompt(topic, placeholder)
    return system, user


def economic_humanizer_instruction_preview(topic: str) -> str:
    return build_gemini_humanizer_instruction(topic, "[FACT SHEET PLACEHOLDER]")


# ---------------------------------------------------------------------------
# CaptionEngine
# ---------------------------------------------------------------------------

class CaptionEngine:
    """Dual-LLM relay (Gemini research -> Claude polish) or economic single-Gemini path."""

    def __init__(
        self,
        *,
        gemini_key: str | None = None,
        anthropic_key: str | None = None,
        research_model: str | None = None,
        writer_model: str | None = None,
    ) -> None:
        g_key = gemini_key or app_config.GEMINI_API_KEY
        a_key = anthropic_key or app_config.ANTHROPIC_API_KEY
        if not g_key:
            raise ValueError("Gemini API key missing. Set GEMINI_API_KEY.")
        # v1beta -> v1 fallback; SDK sends x-goog-api-key header automatically.
        self._gemini = make_gemini_client_with_fallback(g_key)

        # Handshake: validate research model against live models.list().
        research_hint = research_model or app_config.GEMINI_RESEARCH_MODEL
        # Ordered preference: 2.5-flash (fastest), then 2.5-pro as first fallback.
        active_research = get_active_model_id(
            self._gemini,
            preference=["2.5-flash", "2.5-pro"],
            capability_type="text",
        )
        self._text_model_chain = build_model_chain(
            self._gemini,
            capability_type="text",
            preferred=research_hint,
        )
        # If the env-hinted model is not live, use the handshake result.
        if not self._text_model_chain or self._text_model_chain[0] != active_research:
            self._text_model_chain = [active_research] + [
                m for m in self._text_model_chain if m != active_research
            ]

        self._econ_gemini_chain = build_model_chain(
            self._gemini,
            capability_type="text",
            preferred=app_config.GEMINI_ECONOMIC_BRAIN_MODEL,
        )
        self._research_model = self._text_model_chain[0] if self._text_model_chain else active_research

        # Headers sent on every request: x-api-key (auto from api_key)
        # + anthropic-version (required; defaults to 2023-06-01 from config).
        self._anthropic = (
            Anthropic(
                api_key=a_key,
                default_headers={
                    "anthropic-version": app_config.ANTHROPIC_API_VERSION,
                    "x-api-key": a_key,
                },
            )
            if a_key
            else None
        )

        # Dynamic Claude model selection via Models API.
        if writer_model:
            self._writer_model = writer_model
        else:
            self._writer_model = app_config.get_best_claude_model(self._anthropic)

        logger.debug(
            "CaptionEngine | Gemini primary: %s (chain len %d) | Claude: %s",
            self._research_model,
            len(self._text_model_chain),
            self._writer_model,
        )

    @property
    def research_primary_id(self) -> str:
        return self._research_model

    def synthesize_facts(
        self,
        topic: str,
        pdf_bundle: dict[str, str],
        *,
        research_model_override: str | None = None,
        variation_index: int = 0,
        total_variants: int = 1,
    ) -> str:
        vnote = ""
        if total_variants > 1:
            vnote = (
                f"Additional angle: emphasize nuance #{variation_index + 1} of {total_variants} "
                "while staying excerpt-faithful."
            )
        instruction = build_gemini_researcher_instruction(topic, variation_notes=vnote)

        context = corpus_to_prompt_context(pdf_bundle)
        if len(context) > _MAX_PROMPT_CHARS:
            context = context[:_MAX_PROMPT_CHARS]

        model = research_model_override or self._research_model
        chain = chain_with_preferred_first(self._text_model_chain, model)
        response = generate_content_with_model_fallback(
            self._gemini,
            chain,
            contents=[instruction, "\n\nPDF CORPUS BEGIN\n", context, "\nPDF CORPUS END"],
        )

        text_attr = getattr(response, "text", None)
        raw_text = text_attr() if callable(text_attr) else text_attr
        if not raw_text:
            parts_out: list[str] = []
            for candidate in getattr(response, "candidates", []) or []:
                content = getattr(candidate, "content", None)
                if not content:
                    continue
                for part in getattr(content, "parts", []) or []:
                    text_val = getattr(part, "text", None)
                    if text_val:
                        parts_out.append(text_val)
            raw_text = "\n".join(parts_out)

        return raw_text.strip() if raw_text else ""

    def synthesize_facts_batch(
        self,
        topic: str,
        pdf_bundle: dict[str, str],
        *,
        num_variants: int | None = None,
    ) -> list[str]:
        """
        Generate all variant narratives in a SINGLE Gemini API call (the One-Call Rule).

        Returns a list of ``num_variants`` raw fact-sheet strings. If Gemini returns
        fewer narratives than requested, the list is padded with empty strings so
        callers can safely iterate with a variant index.

        Falls back to an empty list on API failure, letting the caller revert to
        per-variant ``synthesize_facts()`` calls.
        """
        count = num_variants or BATCH_DEFAULT_SIZE
        instruction = build_batch_researcher_instruction(topic, count)

        context = corpus_to_prompt_context(pdf_bundle)
        if len(context) > _MAX_PROMPT_CHARS:
            context = context[:_MAX_PROMPT_CHARS]

        chain = list(self._text_model_chain)
        try:
            response = generate_content_with_model_fallback(
                self._gemini,
                chain,
                contents=[instruction, "\n\nPDF CORPUS BEGIN\n", context, "\nPDF CORPUS END"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Batch research API call failed: %s", exc)
            return []

        text_attr = getattr(response, "text", None)
        raw_text = text_attr() if callable(text_attr) else text_attr
        if not raw_text:
            parts_out: list[str] = []
            for candidate in getattr(response, "candidates", []) or []:
                content = getattr(candidate, "content", None)
                if not content:
                    continue
                for part in getattr(content, "parts", []) or []:
                    text_val = getattr(part, "text", None)
                    if text_val:
                        parts_out.append(text_val)
            raw_text = "\n".join(parts_out)

        if not raw_text:
            logger.warning("Batch research returned empty response for topic '%s'.", topic)
            return []

        narratives = _parse_batch_narratives(raw_text.strip(), count)
        logger.info(
            "Batch research complete: %d/%d narratives parsed for topic '%s'.",
            sum(1 for n in narratives if n),
            count,
            topic,
        )
        return narratives

    def humanize_voice(
        self,
        raw_fact_sheet: str,
        topic: str,
        *,
        variation_index: int = 0,
        total_variants: int = 1,
        cta_keyword: str | None = None,
    ) -> str:
        if not self._anthropic:
            raise ValueError(
                "Anthropic client unavailable. Add ANTHROPIC_API_KEY or enable economic_brain_mode."
            )
        system_prompt = build_claude_humanizer_system_prompt("Anna")
        user_prompt = build_claude_humanizer_user_prompt(
            topic,
            raw_fact_sheet,
            variation_index=variation_index,
            total_variants=total_variants,
            cta_keyword=cta_keyword,
        )

        message = self._anthropic.messages.create(
            model=self._writer_model,
            max_tokens=900,
            temperature=0.55,
            system=system_prompt,
            messages=[{"role": "user", "content": [{"type": "text", "text": user_prompt}]}],
        )

        paragraphs: list[str] = []
        for block in getattr(message, "content", []) or []:
            text_chunk = getattr(block, "text", None)
            if text_chunk:
                paragraphs.append(text_chunk)
        return "\n\n".join(paragraphs).strip()

    def humanize_voice_gemini(
        self,
        raw_fact_sheet: str,
        topic: str,
        *,
        variation_index: int = 0,
        total_variants: int = 1,
        model_id: str | None = None,
        cta_keyword: str | None = None,
    ) -> str:
        prompt = build_gemini_humanizer_instruction(
            topic,
            raw_fact_sheet,
            variation_index=variation_index,
            total_variants=total_variants,
            cta_keyword=cta_keyword,
        )
        econ = model_id or app_config.GEMINI_ECONOMIC_BRAIN_MODEL
        chain = chain_with_preferred_first(self._econ_gemini_chain, econ)
        response = generate_content_with_model_fallback(self._gemini, chain, contents=[prompt])

        text_attr = getattr(response, "text", None)
        caption = text_attr() if callable(text_attr) else text_attr
        if not caption:
            parts_out: list[str] = []
            for cand in getattr(response, "candidates", []) or []:
                content = getattr(cand, "content", None)
                if not content:
                    continue
                for part in getattr(content, "parts", []) or []:
                    t = getattr(part, "text", None)
                    if t:
                        parts_out.append(t)
            caption = "\n".join(parts_out)

        return (caption or "").strip()

    def humanize_voice_with_fallback(
        self,
        raw_fact_sheet: str,
        topic: str,
        *,
        variation_index: int = 0,
        total_variants: int = 1,
        cta_keyword: str | None = None,
        economic: bool = False,
        model_id: str | None = None,
    ) -> tuple[str, str]:
        """
        Try the primary humanizer (Claude if premium, Gemini if economic).
        On failure, automatically retries with Gemini fallback chain.

        Returns (caption, mode_tag) where mode_tag is one of:
          "humanized"           -- primary succeeded
          "gemini_fallback"     -- Claude failed, Gemini succeeded
          "researcher_fallback" -- all LLMs failed (returns empty string)
        """
        kw = cta_keyword or contextual_cta_keyword(topic)

        if economic:
            try:
                result = self.humanize_voice_gemini(
                    raw_fact_sheet, topic,
                    variation_index=variation_index,
                    total_variants=total_variants,
                    model_id=model_id,
                    cta_keyword=kw,
                )
                if result:
                    return result, "humanized"
            except Exception as exc:  # noqa: BLE001
                logger.warning("Gemini humanizer failed (economic mode): %s", exc)
            return "", "researcher_fallback"

        # Premium path: Claude first, then Gemini fallback.
        if self._anthropic:
            try:
                result = self.humanize_voice(
                    raw_fact_sheet, topic,
                    variation_index=variation_index,
                    total_variants=total_variants,
                    cta_keyword=kw,
                )
                if result:
                    return result, "humanized"
            except Exception as claude_exc:  # noqa: BLE001
                logger.warning(
                    "Claude humanizer failed (%s). Retrying with Gemini fallback.", claude_exc
                )

        try:
            result = self.humanize_voice_gemini(
                raw_fact_sheet, topic,
                variation_index=variation_index,
                total_variants=total_variants,
                cta_keyword=kw,
            )
            if result:
                return result, "gemini_fallback"
        except Exception as gem_exc:  # noqa: BLE001
            logger.warning("Gemini fallback humanizer also failed: %s", gem_exc)

        return "", "researcher_fallback"

    def relay(
        self,
        topic: str,
        pdf_bundle: dict[str, str],
        *,
        economic_brain_mode: bool = False,
        variation_index: int = 0,
        total_variants: int = 1,
    ) -> tuple[str, str]:
        if not economic_brain_mode and not self._anthropic:
            raise ValueError("ANTHROPIC_API_KEY required for premium relay or set economic_brain_mode=True.")
        if economic_brain_mode:
            econ_model = app_config.GEMINI_ECONOMIC_BRAIN_MODEL
            raw = self.synthesize_facts(
                topic,
                pdf_bundle,
                research_model_override=econ_model,
                variation_index=variation_index,
                total_variants=total_variants,
            )
            caption = (
                self.humanize_voice_gemini(
                    raw,
                    topic,
                    variation_index=variation_index,
                    total_variants=total_variants,
                    model_id=econ_model,
                )
                if raw
                else ""
            )
            return raw, caption

        raw = self.synthesize_facts(
            topic,
            pdf_bundle,
            variation_index=variation_index,
            total_variants=total_variants,
        )
        caption = (
            self.humanize_voice(
                raw,
                topic,
                variation_index=variation_index,
                total_variants=total_variants,
            )
            if raw
            else ""
        )
        return raw, caption
