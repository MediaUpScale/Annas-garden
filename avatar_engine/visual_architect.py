# -*- coding: utf-8 -*-
"""
VisualArchitect -- cinematic image-prompt builder for the Anna persona.

All scene libraries (environments, angles, actions, times-of-day) are loaded
exclusively from avatar_engine/master_dna.json via the persona_dna module.
Zero hard-coding of keywords, environments, or themes here.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config as app_config
from persona_dna import (
    ANNA_ACTIONS,
    CAMERA_ANGLES,
    ENVIRONMENTS,
    LEGACY_RULE,
    TIMES_OF_DAY,
    visual_style_block,
)

_QUALITY_SUFFIX = (
    "Ultra-realistic. Shot on 35mm film or high-end smartphone candid aesthetic. "
    "No AI skin blur, no 'plastic' smoothing. Visible pores, natural fine lines, healthy inner glow. "
    "Physically accurate lighting, restrained colour grading. "
    "INDISTINGUISHABLE FROM A REAL PHOTOGRAPH."
)


class VisualArchitect:
    """Translate health-topic briefs into high-variance, photoreal image prompts."""

    def build_prompt(
        self,
        topic_brief: str,
        *,
        aspect_ratio: str | None = None,
        variation_index: int = 0,
        total_variants: int = 1,
        force_kid: bool | None = None,
    ) -> str:
        """
        Build a cinematography-ready image prompt.

        Parameters
        ----------
        topic_brief:
            The health/protocol topic (e.g. 'Celtic Salt hydration').
        aspect_ratio:
            Override the config default (e.g. '3:4').
        variation_index:
            Used to seed deterministic variation spread across a batch.
        total_variants:
            Total size of the batch (for variant labelling).
        force_kid:
            True/False override; None = probabilistic (15% per Legacy Rule).
        """
        rng = random.Random()  # fresh RNG each call -- full randomness

        env = rng.choice(ENVIRONMENTS) if ENVIRONMENTS else {
            "name": "Wild Fields",
            "desc": "Open meadow, golden-hour back-light."
        }
        angle = rng.choice(CAMERA_ANGLES) if CAMERA_ANGLES else "Medium close-up"
        action = rng.choice(ANNA_ACTIONS) if ANNA_ACTIONS else "harvesting herbs by hand"
        time_of_day = rng.choice(TIMES_OF_DAY) if TIMES_OF_DAY else "Golden hour"
        ratio = aspect_ratio or app_config.GEMINI_IMAGE_ASPECT_RATIO

        legacy_prob = LEGACY_RULE.get("Probability", 0.15)
        include_kid = force_kid if force_kid is not None else (rng.random() < legacy_prob)
        kid_desc = LEGACY_RULE.get(
            "Description",
            "A young grandchild (toddler, 2-4 years old) is present nearby.",
        )
        kid_line = f"\nLEGACY ELEMENT: {kid_desc}" if include_kid else ""

        variant_note = ""
        if total_variants > 1:
            variant_note = (
                f"\nCreative variant {variation_index + 1} of {total_variants}. "
                "Preserve likeness fidelity while maximising compositional freshness."
            )

        visual = visual_style_block()

        return (
            f"{visual}\n\n"
            f"TOPIC: {topic_brief.strip()}\n\n"
            f"ENVIRONMENT: {env['name']}\n{env['desc']}\n\n"
            f"CAMERA ANGLE: {angle}\n\n"
            f"TIME OF DAY / LIGHTING: {time_of_day}\n\n"
            f"ANNA'S ACTION: Anna is {action}\n"
            f"{kid_line}"
            f"{variant_note}\n\n"
            f"Aspect ratio target: {ratio}\n\n"
            f"{_QUALITY_SUFFIX}"
        )
