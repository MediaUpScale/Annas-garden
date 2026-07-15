# -*- coding: utf-8 -*-
"""Small string helpers shared across avatar_engine."""


def subject_slug(subject: str) -> str:
    """Filesystem-safe slug for outputs/assets/<slug>/."""
    raw = subject.strip().lower()
    out = "".join(ch if ch.isalnum() else "_" for ch in raw)
    parts = [p for p in out.split("_") if p]
    return "_".join(parts) if parts else "general"
