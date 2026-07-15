# -*- coding: utf-8 -*-
"""
Anna persona -- loaded exclusively from avatar_engine/master_dna.json.

This module is the single Python interface layer for all persona data.
All prompt builders, the visual architect, and the humanizer import from here.
Zero hard-coding of keywords, themes, or philosophy is permitted in this file;
every value originates from master_dna.json.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

_MASTER_DNA_PATH = Path(__file__).resolve().parent / "avatar_engine" / "master_dna.json"


def _load() -> dict:
    return json.loads(_MASTER_DNA_PATH.read_text(encoding="utf-8"))


_P = _load()

# ---------------------------------------------------------------------------
# Core identity
# ---------------------------------------------------------------------------

ANNA_DISPLAY_NAME: str = _P.get("Subject", "Anna")
ANNA_AGE_YEARS: int = _P.get("Physical_Appearance", {}).get("Age", 72)

_phys = _P.get("Physical_Appearance", {})
ANNA_PHYSICAL: str = (
    f"{ANNA_AGE_YEARS}-year-old holistic authority: "
    f"{_phys.get('Hair', 'natural silver hair')}. "
    f"Physique: {_phys.get('Physique', '')}. "
    f"Skin: {_phys.get('Skin_Texture', '')} "
    f"Visual standard: {_phys.get('Visual_Standard', '')}"
)

_voice = _P.get("Brand_Voice_&_Mission", {})
ANNA_VOICE_PRINCIPLES: str = (
    f"Tone: {_voice.get('Tone', '')}. "
    f"Mission: {_voice.get('Mission_Statement', '')}. "
    f"Themes: {_voice.get('Themes', '')}. "
    "Never markets with AI-shaped hype words (no 'unlock', 'discover', 'game-changer'). "
    "Hooks are decisive and specific."
)

ANNA_EXPERTISE_LENS: str = (
    f"Target audience: {_voice.get('Target_Audience', 'USA Premium Market')}. "
    "Translates biochemical mechanisms into disciplined, humane guidance for a "
    "premium longevity audience."
)

CORE_DIRECTIVE: str = _P.get("Core_Directive", "")
CORE_MECHANICS: list[str] = _P.get("Core_Natural_Mechanics", [])

# ---------------------------------------------------------------------------
# Philosophical core (Barbara O'Neill-inspired; name never used in captions)
# ---------------------------------------------------------------------------

_PHIL = _P.get("Philosophical_Core", {})
PHILOSOPHY_CORE_BELIEF: str = _PHIL.get("Core_Belief", "")
PHILOSOPHY_THE_CONFLICT: str = _PHIL.get("The_Conflict", "")
PHILOSOPHY_SCIENCE_METAPHORS: list[str] = _PHIL.get("Science_Metaphors", [])
PHILOSOPHY_VOICE_ADJECTIVES: list[str] = _PHIL.get("Voice_Adjectives", [])
PHILOSOPHY_BANNED_NAMES: list[str] = _PHIL.get("Banned_Name_References", [])

# ---------------------------------------------------------------------------
# Narrative angles for batch research
# ---------------------------------------------------------------------------

NARRATIVE_ANGLES: list[dict] = _P.get("Narrative_Angles", [])

_BATCH = _P.get("Batch_Research", {})
BATCH_DEFAULT_SIZE: int = _BATCH.get("Default_Batch_Size", 20)
BATCH_DELIMITER_OPEN: str = _BATCH.get("Delimiter_Open", "===NARRATIVE_")
BATCH_DELIMITER_CLOSE: str = _BATCH.get("Delimiter_Close", "===")
BATCH_WORDS_PER_NARRATIVE: str = _BATCH.get("Words_Per_Narrative", "150-250")
BATCH_ROTATION_PATTERN: str = _BATCH.get("Rotation_Pattern", "")

# ---------------------------------------------------------------------------
# CTA inventory
# ---------------------------------------------------------------------------

_CTA_INV = _P.get("Call_To_Action_Inventory", {})
CTA_KEYWORDS: list[str] = _CTA_INV.get("Authorized_Keywords", [])
_THEME_MAP: dict[str, str] = _CTA_INV.get("Theme_Keyword_Map", {})
CTA_VOICE_INSTRUCTION: str = _CTA_INV.get("CTA_Voice_Instruction", "")

# ---------------------------------------------------------------------------
# Visual / image data
# ---------------------------------------------------------------------------

ENVIRONMENTS: list[dict] = _P.get("Environments", [])
CAMERA_ANGLES: list[str] = _P.get("Camera_Angles", [])
TIMES_OF_DAY: list[str] = _P.get("Times_Of_Day", [])
ANNA_ACTIONS: list[str] = _P.get("Anna_Actions", [])
LEGACY_RULE: dict = _P.get("Legacy_Rule", {"Probability": 0.15})

# ---------------------------------------------------------------------------
# CTA keyword selection
# ---------------------------------------------------------------------------


def contextual_cta_keyword(topic: str) -> str:
    """
    Select the CTA keyword most relevant to the post topic.

    Scans the topic string against every key in Theme_Keyword_Map (longest
    keys first to avoid short-fragment collisions), returns the mapped keyword
    on first match.  Falls back to a random keyword if nothing matches.
    """
    topic_lower = topic.lower()
    for fragment in sorted(_THEME_MAP.keys(), key=len, reverse=True):
        if fragment in topic_lower:
            return _THEME_MAP[fragment]
    return random_cta_keyword()


def random_cta_keyword() -> str:
    """Return one random authorized CTA keyword (e.g. 'SALT', 'GINGER')."""
    return random.choice(CTA_KEYWORDS) if CTA_KEYWORDS else "RESTORE"


# ---------------------------------------------------------------------------
# Prompt context helpers
# ---------------------------------------------------------------------------


def philosophy_block() -> str:
    """
    Philosophy constraint block injected into researcher and humanizer prompts.
    Encodes Anna's core conflict (natural healing vs synthetic pharma) and
    the science metaphors she uses. Never references source author names.
    """
    metaphors = "\n".join(f"  - {m}" for m in PHILOSOPHY_SCIENCE_METAPHORS)
    angles = "\n".join(
        f"  - {a.get('code', '')}: {a.get('description', '')}"
        for a in NARRATIVE_ANGLES
    )
    banned = ", ".join(f'"{n}"' for n in PHILOSOPHY_BANNED_NAMES)
    return (
        f"PHILOSOPHY CORE:\n"
        f"  Belief: {PHILOSOPHY_CORE_BELIEF}\n"
        f"  The Conflict: {PHILOSOPHY_THE_CONFLICT}\n\n"
        f"SCIENCE METAPHORS (use when relevant):\n{metaphors}\n\n"
        f"NARRATIVE ANGLES AVAILABLE:\n{angles}\n\n"
        f"BANNED NAME REFERENCES (never appear in any output): {banned}"
    )


def persona_context_block() -> str:
    """Compact block injected into every LLM research/humanizer prompt."""
    mechanics = "\n".join(f"  - {m}" for m in CORE_MECHANICS)
    return (
        f"Identity: {ANNA_DISPLAY_NAME}, age {ANNA_AGE_YEARS}.\n"
        f"Core directive: {CORE_DIRECTIVE}\n"
        f"Physical: {ANNA_PHYSICAL}\n"
        f"Audience lens: {ANNA_EXPERTISE_LENS}\n"
        f"Voice: {ANNA_VOICE_PRINCIPLES}\n"
        f"Core natural mechanics:\n{mechanics}\n\n"
        f"{philosophy_block()}"
    )


def visual_style_block() -> str:
    """Compact visual / environment block for image-prompt builders."""
    env = _P.get("Visual_Environment_&_Composition", {})
    phys = _P.get("Physical_Appearance", {})
    env_names = [e["name"] for e in ENVIRONMENTS]
    lines = [
        f"Subject: {ANNA_DISPLAY_NAME}, age {ANNA_AGE_YEARS}. {phys.get('Hair', '')}.",
        f"Physique: {phys.get('Physique', '')}.",
        f"Skin realism: {phys.get('Skin_Texture', '')}",
        f"Visual standard: {phys.get('Visual_Standard', '')}",
        f"Atmosphere: {env.get('Atmosphere', 'Dramatic, atmospheric, grounded in nature.')}",
        f"Available environments: {'; '.join(env_names)}",
    ]
    return "\n".join(lines)
