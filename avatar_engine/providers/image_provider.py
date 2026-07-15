# -*- coding: utf-8 -*-
"""
Image generation abstraction plus Gemini adapter (Nano Banana Pro / Gemini Image).

Additional providers subclass ``ImageProvider`` without rewriting the orchestrator.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from google import genai
from google.genai import errors as genai_errors
from PIL import Image

import config as app_config
from avatar_engine.providers.gemini_utils import (
    build_model_chain,
    chain_with_preferred_first,
    is_model_not_found_error,
)

logger = logging.getLogger(__name__)


def _iterate_response_parts(response: Any) -> Iterable[Any]:
    parts = getattr(response, "parts", None)
    if parts:
        yield from parts
        return
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return
    content = getattr(candidates[0], "content", None)
    if content is None:
        return
    yield from getattr(content, "parts", []) or []


def _generation_config_for_image(aspect_ratio: str):
    """Build SDK config when ``ImageConfig`` is available."""
    try:
        from google.genai import types  # type: ignore

        return types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
        )
    except Exception:  # noqa: BLE001
        return None


class ImageProvider(ABC):
    @abstractmethod
    def generate(
        self,
        prompt: str,
        *,
        reference_image_path: Path | None = None,
        output_stem: str = "avatar_post",
        output_directory: Path | None = None,
    ) -> Path:
        """Return filesystem path of the rendered asset."""


class GeminiImageAdapter(ImageProvider):
    def __init__(self, api_key: str | None = None, model_id: str | None = None) -> None:
        key = api_key or app_config.GEMINI_API_KEY
        if not key:
            raise ValueError("Gemini API key missing. Set GEMINI_API_KEY.")
        self._client = genai.Client(api_key=key)
        preferred = model_id or app_config.GEMINI_IMAGE_MODEL
        self._image_chain = build_model_chain(
            self._client,
            capability_type="image",
            preferred=preferred,
        )
        self._model_id = self._image_chain[0] if self._image_chain else preferred
        self.last_gemini_image_model_used: str | None = None
        self.last_gemini_image_failure_model_id: str | None = None
        logger.debug(
            "GeminiImageAdapter primary: %s (image chain length %d)",
            self._model_id,
            len(self._image_chain),
        )

    def generate(
        self,
        prompt: str,
        *,
        reference_image_path: Path | None = None,
        output_stem: str = "avatar_post",
        output_directory: Path | None = None,
        aspect_ratio: str | None = None,
    ) -> Path:
        contents: list[Any] = []

        ratio = aspect_ratio or app_config.GEMINI_IMAGE_ASPECT_RATIO
        ratio_note = (
            f"Compose for a strict {ratio} portrait frame (vertical social feed), "
            "full-bleed subject with tasteful headroom and foot room."
        )
        prompt_with_ratio = f"{ratio_note}\n\n{prompt}"

        ref_path = reference_image_path if reference_image_path is not None else app_config.REFERENCE_IMAGE_PATH
        path_obj = Path(ref_path)

        if ref_path and path_obj.exists():
            reference_prompt = (
                "Use the uploaded reference portrait ONLY to preserve facial identity across the frame: "
                "bone structure, age cues, complexion, hairstyle. Recreate wardrobe and staging from prompt. "
                "Do not caricature.\n\n"
            )
            contents.extend([reference_prompt + prompt_with_ratio, Image.open(path_obj)])
        else:
            if ref_path:
                logger.warning(
                    "Reference likeness path configured but missing on disk (%s); sending text-only image prompt.",
                    path_obj.resolve(),
                )
            contents.append(prompt_with_ratio)

        gen_cfg = _generation_config_for_image(ratio)
        chain = chain_with_preferred_first(self._image_chain, self._model_id)

        self.last_gemini_image_model_used = None
        self.last_gemini_image_failure_model_id = None

        response = None
        last_err: BaseException | None = None
        chosen_model: str | None = None
        for candidate in chain:
            self.last_gemini_image_failure_model_id = candidate
            if gen_cfg is not None:
                try:
                    response = self._client.models.generate_content(
                        model=candidate,
                        contents=contents,
                        config=gen_cfg,
                    )
                    chosen_model = candidate
                    logger.info("Gemini OK | generate_content (image) | model=%s", candidate)
                    break
                except genai_errors.APIError as exc:
                    last_err = exc
                    if is_model_not_found_error(exc):
                        logger.warning(
                            "GEMINI_ALERT (image): `%s` unavailable (%s); trying next SKU.",
                            candidate,
                            exc,
                        )
                        continue
                    logger.warning(
                        "Image ImageConfig rejected (%s); retry without ImageConfig on %s.", exc, candidate,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    if is_model_not_found_error(exc):
                        logger.warning(
                            "GEMINI_ALERT (image): `%s` unavailable (%s); trying next SKU.", candidate, exc,
                        )
                        continue
                    logger.warning(
                        "Image generation with ImageConfig failed (%s); retry without config on %s.", exc, candidate,
                    )
            try:
                response = self._client.models.generate_content(model=candidate, contents=contents)
                chosen_model = candidate
                logger.info("Gemini OK | generate_content (image) | model=%s", candidate)
                break
            except genai_errors.APIError as exc:
                last_err = exc
                if is_model_not_found_error(exc):
                    logger.warning(
                        "GEMINI_ALERT (image): `%s` unavailable (%s); trying next SKU.", candidate, exc,
                    )
                    continue
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if is_model_not_found_error(exc):
                    logger.warning(
                        "GEMINI_ALERT (image): `%s` unavailable (%s); trying next SKU.", candidate, exc,
                    )
                    continue
                raise

        if chosen_model:
            self.last_gemini_image_model_used = chosen_model

        if response is None and last_err:
            raise last_err
        if response is None:
            raise RuntimeError("No Gemini image model in chain returned a response.")

        out_dir = output_directory or app_config.OUTPUTS_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        slug = "".join(ch if ch.isalnum() else "_" for ch in output_stem).strip("_") or "generated"
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
        out_path = out_dir / f"{slug}_{ts}.png"

        saved = False
        for part in _iterate_response_parts(response):
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None) if inline else None
            if data:
                with out_path.open("wb") as handle:
                    handle.write(data if isinstance(data, (bytes, bytearray)) else bytes(data))
                saved = True
                break
            if hasattr(part, "as_image"):
                pil_image = part.as_image()
                pil_image.save(out_path)
                saved = True
                break

        if not saved:
            raise RuntimeError("Gemini image response contained no downloadable image payload.")

        return out_path.resolve()
