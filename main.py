# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path


def _repair_drive_text_file(file_path: Path) -> bool:
    """
    Re-encode a UTF-16 file (Google Drive desktop corruption) as clean UTF-8,
    or strip stray NUL bytes introduced by sync.

    Handles:
    - UTF-16-LE / UTF-16-BE with explicit BOM
    - UTF-16-LE without BOM (Python files starting '#\\x00', JSON files starting '{\\x00')
    - Files with embedded NUL bytes but otherwise valid UTF-8
    """
    if not file_path.is_file():
        return False
    raw = file_path.read_bytes()

    text = ""
    if raw.startswith(b"\xff\xfe"):
        text = raw[2:].decode("utf-16-le")
    elif raw.startswith(b"\xfe\xff"):
        text = raw[2:].decode("utf-16-be")
    elif raw.startswith((b"#\x00 ", b"#\x00-")):
        # Python source in UTF-16-LE without BOM
        text = raw.decode("utf-16-le")
    elif raw.startswith(b"{\x00") or raw.startswith(b"[\x00"):
        # JSON file in UTF-16-LE without BOM
        text = raw.decode("utf-16-le")

    if text:
        file_path.write_text(text, encoding="utf-8", newline="\n")
        return True

    if b"\x00" not in raw:
        return False
    file_path.write_bytes(raw.replace(b"\x00", b""))
    return True


def _clean_all_python_sources(engine_root: Path) -> None:
    """
    Glob every .py and .json in the project and repair Drive-sync encoding issues.
    Runs silently at bootstrap before any imports that touch persona/config files.
    """
    for pattern in ("*.py", "*.json"):
        for candidate in sorted(engine_root.rglob(pattern)):
            if _repair_drive_text_file(candidate):
                try:
                    rel = candidate.relative_to(engine_root)
                except ValueError:
                    rel = candidate
                print(f"[bootstrap] Repaired {rel}", file=sys.stderr)


_ENGINE_ROOT_BOOT = Path(__file__).resolve().parent
_clean_all_python_sources(_ENGINE_ROOT_BOOT)


import argparse
import logging
from datetime import datetime, timezone
from typing import Any

from google import genai

import config as app_config
from avatar_engine.caption_engine import (
    CaptionEngine,
    build_gemini_researcher_instruction,
    economic_humanizer_instruction_preview,
    humanizer_preview_with_placeholder,
    build_batch_researcher_instruction,
)
from avatar_engine.imgbb_client import upload_image_file_to_imgbb
from avatar_engine.content_library import (
    append_entry,
    build_library_metadata,
    dump_raw_research_to_log,
)
from avatar_engine.durable_library import (
    PENDING_CAPTION,
    merge_update_json,
    path_under_engine,
    write_atomic_json,
)
from avatar_engine.knowledge.pdf_loader import list_pdf_relative_paths, load_digital_product_corpus
from avatar_engine.post_planner import (
    append_planner_row,
    append_postplanner_xlsx_row,
    scheduled_bulk_post_display,
    update_planner_row,
)
from avatar_engine.providers.gemini_utils import build_model_chain, get_latest_model
from persona_dna import contextual_cta_keyword
from avatar_engine.providers.image_provider import GeminiImageAdapter
from avatar_engine.subject_brain import imagine_subject, imagine_subject_instruction_preview
from avatar_engine.text_utils import subject_slug
from avatar_engine.visual_architect import VisualArchitect
from run_ledger import (
    PlannedModels,
    activate_run_ledger,
    configure_file_logging,
    ledger_file_path,
)


_LOG = logging.getLogger(__name__)


def _fallback_caption_from_research_facts(raw_sheet: str) -> str:
    """Humanizer-offline path: short header + raw fact sheet body."""
    body = raw_sheet.strip() if isinstance(raw_sheet, str) else ""
    if not body:
        return ""
    return "[Caption from researcher output - humanizer skipped]" + chr(10) + chr(10) + body

def _silence_noisy_http_loggers() -> None:
    """Quiet httpx / anthropic / google SDK chatter without muting ``__main__`` run logs."""
    for name in (
        "anthropic",
        "httpx",
        "google",
        "httpcore",
        "google.genai",
        "google_genai",
        "google.auth",
        "google.cloud",
        "google.api_core",
        "urllib3",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


def _looks_like_upstream_api_failure(exc: BaseException) -> bool:
    """Heuristic: Anthropic / Gemini transport & API errors."""
    mod = getattr(type(exc), "__module__", "") or ""
    if mod.startswith("anthropic"):
        return True
    if "anthropic." in mod:
        return True
    if "google.genai" in mod:
        return True
    return mod.startswith("httpx.") or exc.__class__.__name__.endswith("HTTPError")


def _color(text: str, code: str) -> str:
    if not getattr(sys.stdout, "isatty", lambda: False)():
        return text
    return f"{code}{text}\033[0m"


def _emit_clean_api_error(exc: BaseException) -> None:
    red = "\033[91m"
    dim = "\033[2m"
    reset = "\033[0m"
    title = type(exc).__name__
    body = getattr(exc, "message", None) or str(exc).strip() or repr(exc)
    print()
    print(_color("API request failed", red))
    print(_color(title, red))
    snippet = body[:2000] + ("\u2026" if len(body) > 2000 else "")
    if not snippet:
        snippet = repr(exc)
    print(dim + snippet + reset)
    print(dim + "Detail: see logs/run_*.log in this project." + reset)
    print()


def _print_production_summary(envelope: dict[str, Any]) -> None:
    """End-of-run: image paths and captions surfaced for scheduling / paste."""
    green = "\033[92m"
    cyan = "\033[96m"
    yellow = "\033[93m"
    sep = "+" + "=" * 62 + "+"
    topic_display = envelope.get("resolved_subject") or "(subject)"
    rows = envelope.get("items") or []
    if not isinstance(rows, list):
        rows = []

    print()
    print(_color(sep, green))
    print(_color("| PRODUCTION SUMMARY                                            |", cyan))
    print(_color(sep, green))
    print(_color("Topic:", green), topic_display)
    print()

    if not rows:
        print(_color("(no artifact rows emitted)", cyan))
        print(_color(sep, green))
        print()
        return

    for row in rows:
        cap = row.get("caption") or ""
        img = row.get("local_image_path") or row.get("image_path") or ""
        bb = row.get("imgbb_url") or ""
        mode = row.get("caption_mode", "humanized")
        print(_color(sep, green))
        print(_color(f"Variant {row.get('variant_index', '?')}", cyan))
        if mode == "researcher_fallback":
            print(_color("  Note: Caption is raw researcher output (humanizer failed).", yellow))
        print(_color("  Image path:", green), img or "(skipped)")
        if bb:
            print(_color("  ImgBB URL:", green), bb)
        print(_color("  Caption:", green))
        for line in str(cap).splitlines() or ["(empty)"]:
            print(" ", line)
    print(_color(sep, green))
    xlsx_rel = path_under_engine(app_config.ENGINE_ROOT, app_config.POST_PLANNER_XLSX)
    lib_hint = ""
    first = rows[0] if rows else {}
    if isinstance(first, dict) and first.get("library_json_relative"):
        lib_hint = str(first["library_json_relative"])
    print(_color("Records:", green), f"bulk workbook `{xlsx_rel}`" + (f"; library `{lib_hint}`" if lib_hint else ""))
    print()


def _snapshot_verified_models(*, economic_brain_mode: bool) -> PlannedModels:
    """Determine first-hop model IDs (matches CaptionEngine/GeminiImageAdapter chain heads)."""
    gem_key = app_config.GEMINI_API_KEY
    humanizer = (
        f"Gemini `{app_config.GEMINI_ECONOMIC_BRAIN_MODEL}` (captions + research)"
        if economic_brain_mode
        else f"Anthropic Claude `{app_config.CLAUDE_MODEL}`"
    )
    img_pref = app_config.GEMINI_IMAGE_MODEL
    if not gem_key:
        research_pref = (
            app_config.GEMINI_ECONOMIC_BRAIN_MODEL
            if economic_brain_mode
            else app_config.GEMINI_RESEARCH_MODEL
        )
        return PlannedModels(
            image_primary_id=img_pref,
            research_primary_id=research_pref,
            humanizer_summary=humanizer,
        )

    client = genai.Client(api_key=gem_key)
    img_chain = build_model_chain(
        client,
        capability_type="image",
        preferred=img_pref,
    )
    research_pref = (
        app_config.GEMINI_ECONOMIC_BRAIN_MODEL
        if economic_brain_mode
        else app_config.GEMINI_RESEARCH_MODEL
    )
    txt_chain = build_model_chain(
        client,
        capability_type="text",
        preferred=research_pref,
    )

    verified_image = img_chain[0]
    verified_research = txt_chain[0]

    discovery_img = get_latest_model(client, kind="image")
    discovery_txt = get_latest_model(client, kind="text")
    _LOG.info(
        "Gemini discovery | strongest image SKU (or safe default) = `%s`; text = `%s`",
        discovery_img,
        discovery_txt,
    )

    return PlannedModels(
        image_primary_id=verified_image,
        research_primary_id=verified_research,
        humanizer_summary=humanizer,
    )


def _bootstrap_pipeline_intro(
    *,
    economic_brain_mode: bool,
    verified: PlannedModels,
    compact: bool = False,
) -> None:
    """Confirm persona, credentials, likeness, routing, and verified model IDs."""
    app_config.print_dotenv_bootstrap()

    gem_ok = bool(app_config.GEMINI_API_KEY and str(app_config.GEMINI_API_KEY).strip())
    claude_ok = bool(app_config.ANTHROPIC_API_KEY and str(app_config.ANTHROPIC_API_KEY).strip())

    print(f"[bootstrap] Gemini API Key detected: {'Yes' if gem_ok else 'No'}")
    print(f"[bootstrap] Claude API Key detected: {'Yes' if claude_ok else 'No'}")
    print(
        "[bootstrap] Gemini image pipeline:",
        f"{app_config.GEMINI_IMAGE_MODEL}",
        "(aspect",
        f"{app_config.GEMINI_IMAGE_ASPECT_RATIO})",
    )
    if economic_brain_mode:
        print(
            "[bootstrap] Economic brain Gemini preference (research + humanizer):",
            app_config.GEMINI_ECONOMIC_BRAIN_MODEL,
            "(fallback chain rotates on 404; see GEMINI_ALERT log lines)",
        )
    else:
        print(
            "[bootstrap] Premium relay | Gemini researcher preference:",
            app_config.GEMINI_RESEARCH_MODEL,
            "| Claude humanizer:",
            app_config.CLAUDE_MODEL,
        )

    print(f"[bootstrap] Verified Image Model: {verified.image_primary_id}")
    print(f"[bootstrap] Verified Research Model: {verified.research_primary_id}")

    print(f"[bootstrap] Economic brain mode = {economic_brain_mode}")

    if compact:
        if not app_config.reference_avatar_exists():
            print(
                f"[bootstrap] Warning: reference avatar missing at "
                f"{app_config.reference_avatar_resolved_path()} (text-only likeness).",
            )
        return

    dn_path = app_config.PERSONA_DNA_PATH.resolve()
    print(f"[bootstrap] Persona DNA file in use: {dn_path}")
    print(f"[bootstrap] File present on disk: {dn_path.is_file()}")
    canonical = app_config.reference_avatar_resolved_path()
    print("[bootstrap] Likeness locked to avatar_reference/avatar.png at:", canonical)
    print(f"[bootstrap] Reference avatar exists: {app_config.reference_avatar_exists()}")
    app_config.warn_if_reference_avatar_missing()


def produce(
    subject: str | None,
    *,
    quantity: int = 1,
    skip_image: bool = False,
    skip_caption: bool = False,
    test_mode: bool = False,
    economic_brain_mode: bool | None = None,
    bootstrap_models: PlannedModels | None = None,
) -> dict[str, Any]:
    qty = max(1, quantity)
    economic = economic_brain_mode if economic_brain_mode is not None else app_config.ECONOMIC_BRAIN_MODE

    bm = bootstrap_models or _snapshot_verified_models(economic_brain_mode=economic)
    _bootstrap_pipeline_intro(economic_brain_mode=economic, verified=bm, compact=not test_mode)

    envelope: dict[str, Any] = {
        "mode": "test" if test_mode else "live",
        "quantity": qty,
        "economic_brain_mode": economic,
        "items": [],
    }

    pdf_inventory = list_pdf_relative_paths(app_config.DIGITAL_PRODUCTS_PATH)

    if test_mode:
        topic_seed = (subject or "").strip() or "Auto subject imaginer (provide subject for production)"
        _LOG.info("TEST MODE scaffold | topic_hint=%s | quantity=%s", topic_seed, qty)
        print("\n=== TEST MODE - no Gemini or Anthropic network calls ===\n")

        print("--- Knowledge test: PDF corpus inventory ---\n")
        if pdf_inventory:
            for name in pdf_inventory:
                print(f"  - {name}")
        else:
            print(
                f"  (No PDF files under `{app_config.DIGITAL_PRODUCTS_PATH.resolve()}`. "
                "Brain cannot ingest guides until PDFs arrive.)",
            )

        imagine_prompt = imagine_subject_instruction_preview()

        architect = VisualArchitect()
        prompt = architect.build_prompt(topic_seed, variation_index=0, total_variants=qty)
        researcher_instruction = build_gemini_researcher_instruction(topic_seed)
        sys_prompt, usr_prompt = humanizer_preview_with_placeholder(topic_seed)

        envelope["digital_products_pdf_files"] = pdf_inventory
        envelope["imagine_subject_instruction"] = imagine_prompt
        envelope["visual_prompt"] = prompt

        print("\n--- Imagine-subject scaffold (Brain) ---\n")
        print(imagine_prompt)

        print("\n--- Visual test (upstream image prompt) ---\n")
        print(prompt)

        print("\n--- Gemini researcher scaffold ---\n")
        print(researcher_instruction)

        print("\n--- Claude humanizer (system) ---\n")
        print(sys_prompt)

        print("\n--- Claude humanizer (user scaffold; FACT SHEET dynamic in live runs) ---\n")
        print(usr_prompt)

        print("\n--- Economic Gemini-only humanizer scaffold ---\n")
        print(economic_humanizer_instruction_preview(topic_seed))

        if skip_image or skip_caption:
            print(
                "\n[hint] `--skip-image` / `--skip-caption` are informational in `--test`; "
                "scaffolds still print.\n",
            )

        envelope["items"].append(
            {
                "topic": topic_seed,
                "caption": "(dry-run)",
                "local_image_path": "(dry-run)",
                "imgbb_url": "",
                "variant_index": 0,
            }
        )

        return envelope

    corpus = load_digital_product_corpus(
        app_config.DIGITAL_PRODUCTS_PATH,
        chunk_char_limit=app_config.PDF_CHUNK_CHAR_LIMIT,
    )

    _silence_noisy_http_loggers()
    resolved_subject = (subject or "").strip()
    if not resolved_subject:
        resolved_subject = imagine_subject(corpus)

    _LOG.info(
        "PIPELINE LIVE | resolved_subject=%r | qty=%s | economic=%s | skip_image=%s skip_caption=%s",
        resolved_subject,
        qty,
        economic,
        skip_image,
        skip_caption,
    )
    logging.info(
        "Models banner | verified_image=%s | verified_research=%s | humanizer=%s",
        bm.image_primary_id,
        bm.research_primary_id,
        bm.humanizer_summary,
    )

    slug = subject_slug(resolved_subject)
    subject_assets = app_config.ASSETS_DIR / slug
    subject_assets.mkdir(parents=True, exist_ok=True)

    caption_engine: CaptionEngine | None = None
    if not skip_caption:
        try:
            caption_engine = CaptionEngine()
            _LOG.info("CaptionEngine online | Gemini text head=`%s`", caption_engine.research_primary_id)
        except Exception as exc:  # noqa: BLE001
            _LOG.error("CaptionEngine init failed: %s", exc, exc_info=True)
            logging.error(
                "FATAL_BEFORE_EXIT | CaptionEngine init | stage=initialization | Gemini_text_head=%s | err=%s",
                bm.research_primary_id,
                exc,
            )
            raise

    envelope["resolved_subject"] = resolved_subject

    items: list[dict[str, Any]] = []

    # One stamp per produce() call shared across all variants so XLSX rows land in the same file.
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    postplanner_dir = app_config.OUTPUTS_DIR / "postplanner"
    logs_dir = app_config.ENGINE_ROOT / "logs"

    # ------------------------------------------------------------------
    # ONE-CALL BATCH RESEARCH: generate all variant narratives upfront
    # in a single Gemini API call to minimize cost.  On failure, falls
    # back to per-variant calls inside the loop.
    # ------------------------------------------------------------------
    pre_narratives: list[str] = []
    if not skip_caption and not economic and caption_engine is not None:
        try:
            pre_narratives = caption_engine.synthesize_facts_batch(
                resolved_subject, corpus, num_variants=qty
            )
            _LOG.info(
                "ONE-CALL batch research: %d/%d narratives ready for '%s'.",
                sum(1 for n in pre_narratives if n),
                qty,
                resolved_subject,
            )
        except Exception as batch_exc:  # noqa: BLE001
            _LOG.warning(
                "Batch research failed (%s). Falling back to per-variant calls.", batch_exc
            )

    for variant in range(qty):
        stem = f"{slug}_v{variant + 1:02d}"
        variation_index = variant

        caption = "(skipped)"
        raw_sheet = "(skipped)"
        img_path_display: Path | str = "(skipped)"
        caption_mode_tag: str | None = None
        durable_abs: Path | None = None
        planner_row_ix: int | None = None
        cta_kw = contextual_cta_keyword(resolved_subject)

        humanizer_notes = bm.humanizer_summary
        econ_model = app_config.GEMINI_ECONOMIC_BRAIN_MODEL

        adapter: GeminiImageAdapter | None = None
        if not skip_image:
            architect = VisualArchitect()
            image_prompt = architect.build_prompt(
                resolved_subject,
                variation_index=variation_index,
                total_variants=qty,
            )
            try:
                adapter = GeminiImageAdapter()
                img_path_display = adapter.generate(
                    image_prompt,
                    reference_image_path=app_config.REFERENCE_IMAGE_PATH,
                    output_stem=stem,
                    output_directory=subject_assets,
                )
                img_used = adapter.last_gemini_image_model_used or bm.image_primary_id
                logging.info(
                    "Variant %s | IMAGE_OK | model_used=%s | path=%s",
                    variant + 1,
                    img_used,
                    img_path_display,
                )
            except Exception as exc:  # noqa: BLE001
                failed_mid = bm.image_primary_id
                if adapter is not None:
                    failed_mid = adapter.last_gemini_image_failure_model_id or failed_mid
                _LOG.error(
                    "Image generation failed variant %s attempted_model=`%s`",
                    variant + 1,
                    failed_mid,
                    exc_info=True,
                )
                logging.error(
                    "FATAL_BEFORE_EXIT | GeminiImageAdapter | variant=%s | model=`%s` | err=%s",
                    variant + 1,
                    failed_mid,
                    exc,
                )
                raise

        img_ref_engine = ""
        if isinstance(img_path_display, Path):
            img_ref_engine = path_under_engine(app_config.ENGINE_ROOT, img_path_display)

        imgbb_url = ""
        if isinstance(img_path_display, Path) and img_path_display.is_file():
            key_ib = app_config.IMGBB_API_KEY
            if key_ib:
                try:
                    imgbb_url = upload_image_file_to_imgbb(key_ib, img_path_display) or ""
                except Exception as up_exc:  # noqa: BLE001
                    _LOG.warning("ImgBB upload exception (%s): %s", img_path_display.name, up_exc, exc_info=True)
                if not imgbb_url:
                    _LOG.warning("ImgBB upload returned empty URL for %s; planner media column stays blank.", img_path_display.name)
            else:
                _LOG.warning("IMGBB_API_KEY missing; CONTENT: MEDIA stays blank.")

        posting_slot_display = scheduled_bulk_post_display(variant_index=variation_index)

        if not skip_caption:
            assert caption_engine is not None
            caption_mode_tag = "humanized"

            # Use pre-computed batch narrative when available; fall back to
            # individual synthesize_facts() call otherwise (economic mode,
            # batch failure, or variant index beyond batch results).
            batch_narrative = (
                pre_narratives[variation_index]
                if pre_narratives and variation_index < len(pre_narratives) and pre_narratives[variation_index]
                else ""
            )

            if batch_narrative:
                raw_sheet = batch_narrative
                _LOG.debug(
                    "Variant %s: using pre-computed batch narrative (%d chars).",
                    variant + 1,
                    len(raw_sheet),
                )
            else:
                try:
                    if economic:
                        raw_sheet = caption_engine.synthesize_facts(
                            resolved_subject,
                            corpus,
                            research_model_override=econ_model,
                            variation_index=variation_index,
                            total_variants=qty,
                        )
                    else:
                        raw_sheet = caption_engine.synthesize_facts(
                            resolved_subject,
                            corpus,
                            variation_index=variation_index,
                            total_variants=qty,
                        )
                except Exception as rex:  # noqa: BLE001
                    attempted_rid = caption_engine.research_primary_id
                    _LOG.error(
                        "Gemini research failed variant=%s Gemini_head=`%s`",
                        variant + 1,
                        attempted_rid,
                        exc_info=True,
                    )
                    logging.error(
                        "FATAL_BEFORE_EXIT | synthesize_facts | variant=%s | err=%s",
                        variant + 1,
                        rex,
                    )
                    raise

            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            durable_fname = f"post_{stamp}_v{variant + 1:02d}.json"
            durable_abs = app_config.LIBRARY_DIR / durable_fname
            created_iso = datetime.now(timezone.utc).isoformat()
            excel_rel = path_under_engine(app_config.ENGINE_ROOT, app_config.POST_PLANNER_XLSX)

            # Checkpoint: persist what we know before the humanizer runs.
            pending_payload: dict[str, Any] = {
                "topic": resolved_subject,
                "subject_slug": slug,
                "variant_index": variant + 1,
                "quantity_total": qty,
                "economic_brain_mode": economic,
                "image_relative": img_ref_engine,
                "imgbb_url": imgbb_url,
                "library_relative": path_under_engine(app_config.ENGINE_ROOT, durable_abs),
                "excel_relative": excel_rel,
                "raw_fact_sheet": raw_sheet,
                "humanized_caption": PENDING_CAPTION,
                "caption_status": "pending",
                "created_utc": created_iso,
            }
            write_atomic_json(durable_abs, pending_payload)

            caption: str = ""

            # --- Humanizer: Claude -> Gemini fallback -> skip variant on total failure ---
            if raw_sheet:
                caption, caption_mode_tag = caption_engine.humanize_voice_with_fallback(
                    raw_sheet,
                    resolved_subject,
                    variation_index=variation_index,
                    total_variants=qty,
                    cta_keyword=cta_kw,
                    economic=economic,
                    model_id=econ_model if economic else None,
                )
            else:
                caption_mode_tag = "researcher_fallback"

            if caption_mode_tag == "researcher_fallback":
                # All LLMs failed. DO NOT write to Excel or CSV -- skip this variant entirely.
                _LOG.warning(
                    "All humanizers failed for variant %s of '%s'. "
                    "Variant skipped in Excel/CSV; research saved to logs/.",
                    variant + 1,
                    resolved_subject,
                )
                merge_update_json(durable_abs, {
                    "humanized_caption": "",
                    "caption_status": "skipped_humanizer_failure",
                    "humanized_utc": datetime.now(timezone.utc).isoformat(),
                    "imgbb_url": imgbb_url,
                })
                # Jump straight to the research log + library steps; skip planner writes.
            else:
                if caption_mode_tag == "gemini_fallback":
                    _LOG.info(
                        "Claude failed; Gemini fallback succeeded for variant %s.", variant + 1
                    )
                caption_payload: dict[str, Any] = {
                    "humanized_caption": caption,
                    "caption_status": caption_mode_tag,
                    "humanized_utc": datetime.now(timezone.utc).isoformat(),
                    "imgbb_url": imgbb_url,
                }
                try:
                    merge_update_json(durable_abs, caption_payload)
                    # Excel row written ONLY after a successful humanizer result.
                    planner_row_ix = append_planner_row(
                        app_config.POST_PLANNER_XLSX,
                        posting_time=posting_slot_display,
                        caption=caption,
                        url_link="",
                        media_url=imgbb_url,
                        post_type_value="IMAGE",
                        template_path=app_config.BULK_POSTS_TEMPLATE_XLSX,
                    )
                except Exception as fin_exc:  # noqa: BLE001
                    _LOG.warning(
                        "Post-humanizer durable/Excel write failed (variant %s): %s",
                        variant + 1,
                        fin_exc,
                        exc_info=True,
                    )
        if skip_caption:
            caption_mode_tag = "skipped"
            planner_row_ix = append_planner_row(
                app_config.POST_PLANNER_XLSX,
                posting_time=posting_slot_display,
                caption=caption if isinstance(caption, str) else "(skipped)",
                url_link="",
                media_url=imgbb_url,
                post_type_value="IMAGE",
                template_path=app_config.BULK_POSTS_TEMPLATE_XLSX,
            )

        logging.info("--- VARIANT %s | TOPIC `%s` ---", variant + 1, resolved_subject)
        logging.info("RAW FACT SHEET (researcher)\n%s", raw_sheet)
        logging.info("FINAL CAPTION (humanizer)\n%s", caption)

        # Dump raw research to logs/ so the library JSON stays lean.
        if isinstance(raw_sheet, str) and raw_sheet not in ("(skipped)", ""):
            try:
                dump_raw_research_to_log(
                    logs_dir,
                    run_stamp=run_stamp,
                    topic=resolved_subject,
                    variant_index=variant + 1,
                    raw_fact_sheet=raw_sheet,
                )
            except Exception as log_exc:  # noqa: BLE001
                _LOG.warning("Research log write failed: %s", log_exc)

        meta = append_entry(
            app_config.CONTENT_LIBRARY_PATH,
            build_library_metadata(
                topic=resolved_subject,
                final_caption=caption if isinstance(caption, str) else "",
                imgbb_url=imgbb_url,
            ),
        )

        # Write timestamped per-run XLSX row only when caption was successfully humanized.
        if caption_mode_tag not in ("researcher_fallback", "skipped"):
            try:
                xlsx_path = append_postplanner_xlsx_row(
                    postplanner_dir,
                    run_stamp=run_stamp,
                    posting_time=posting_slot_display,
                    caption=caption if isinstance(caption, str) else "",
                    media_url=imgbb_url,
                )
                _LOG.info("PostPlanner XLSX row written: %s", xlsx_path.name)
            except Exception as xlsx_exc:  # noqa: BLE001
                _LOG.warning("PostPlanner XLSX write failed (variant %s): %s", variant + 1, xlsx_exc)

        if skip_image:
            img_report = "(skipped)"
        elif adapter is not None and adapter.last_gemini_image_model_used:
            img_report = adapter.last_gemini_image_model_used
        else:
            img_report = bm.image_primary_id
        lib_json_rel = path_under_engine(app_config.ENGINE_ROOT, durable_abs) if durable_abs else ""
        items.append(
            {
                "topic": resolved_subject,
                "variant_index": variant + 1,
                "local_image_path": img_ref_engine,
                "imgbb_url": imgbb_url,
                "caption": caption if isinstance(caption, str) else "",
                "library_timestamp": meta.get("timestamp"),
                "library_json_relative": lib_json_rel,
                "excel_row": planner_row_ix,
                "model_image_used": img_report,
                "model_research_head": bm.research_primary_id,
                "humanizer": humanizer_notes,
                "caption_mode": caption_mode_tag if caption_mode_tag is not None else "skipped",
            }
        )
    envelope["items"] = items

    snippet_lines: list[str] = []
    for idx, row in enumerate(items):
        cap = row.get("caption")
        if isinstance(cap, str):
            snippet_lines.append(f"{idx + 1}. {cap}")
    snippet = "\n".join(snippet_lines)
    snippet_path = app_config.OUTPUTS_DIR / "last_captions_bundle.txt"
    if snippet.strip():
        snippet_path.write_text(snippet.strip() + "\n", encoding="utf-8")

    _LOG.info("PIPELINE DONE | artifacts under outputs/")
    return envelope


def run_pipeline(
    topic: str,
    *,
    skip_image: bool = False,
    skip_caption: bool = False,
    test_mode: bool = False,
) -> dict[str, Any]:
    """Backward-compatible alias for scripts expecting the older entrypoint."""
    bm = _snapshot_verified_models(economic_brain_mode=app_config.ECONOMIC_BRAIN_MODE)
    return produce(
        topic.strip() if topic else None,
        quantity=1,
        skip_image=skip_image,
        skip_caption=skip_caption,
        test_mode=test_mode,
        economic_brain_mode=None,
        bootstrap_models=bm,
    )


def _print_test_footer() -> None:
    print("\n--- Test summary ---\n")
    print("Dry-run complete; no Gemini or Claude paid calls were exercised for generation.\n")


def cli() -> None:
    parser = argparse.ArgumentParser(description="Holistic Legacy high-output persona factory.")
    parser.add_argument("topic", nargs="?", help='Optional topic/subject ("Castor Oil"). Omit for AI-chosen subjects.')
    parser.add_argument("--quantity", "-n", type=int, default=1, help="Parallel unique visual/caption variations.")
    parser.add_argument("--skip-image", action="store_true", help="Caption + planner only.")
    parser.add_argument("--skip-caption", action="store_true", help="Image synthesis only.")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Dry-run: print scaffold prompts/inventory without calling Gemini or Anthropic APIs.",
    )
    parser.add_argument(
        "--economic",
        dest="economic",
        action="store_true",
        help="Force Gemini-only economic brain mode (research + captions).",
    )
    parser.add_argument(
        "--premium-relay",
        dest="premium",
        action="store_true",
        help="Force Gemini research + Claude 3.5 Sonnet captions (dual-LLM relay).",
    )
    args = parser.parse_args()

    if args.economic and args.premium:
        raise SystemExit("Choose either --economic or --premium-relay, not both.")

    economic_choice: bool | None
    if args.premium:
        economic_choice = False
    elif args.economic:
        economic_choice = True
    else:
        economic_choice = None

    econ_resolved = economic_choice if economic_choice is not None else app_config.ECONOMIC_BRAIN_MODE
    planned_models = _snapshot_verified_models(economic_brain_mode=econ_resolved)

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s | %(message)s",
        force=True,
    )
    _silence_noisy_http_loggers()

    log_path, ts_token = ledger_file_path(app_config.ENGINE_ROOT)
    configure_file_logging(log_path)

    for h in logging.root.handlers:
        if getattr(h, "_engine_run_journal", False):
            continue
        h.setLevel(logging.WARNING)

    activate_run_ledger(log_path, planned=planned_models)
    logging.getLogger(__name__).info("=== ENGINE RUN BEGIN | log=%s | ts=%s ===", log_path, ts_token)

    print(f"[bootstrap] Detailed run log: {log_path}")

    topic_raw = args.topic or None
    if topic_raw is None and args.test:
        topic_raw = input("Topic (optional, press Enter to rely on scaffold placeholder): ").strip() or ""

    envelope: dict[str, Any] | None = None
    try:
        envelope = produce(
            topic_raw,
            quantity=args.quantity,
            skip_image=args.skip_image,
            skip_caption=args.skip_caption,
            test_mode=args.test,
            economic_brain_mode=economic_choice,
            bootstrap_models=planned_models,
        )
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, KeyboardInterrupt):
            raise
        if _looks_like_upstream_api_failure(exc):
            _emit_clean_api_error(exc)
            _LOG.error("Run aborted due to upstream API failure.", exc_info=True)
            sys.exit(1)
        raise

    logging.getLogger(__name__).info(
        "=== ENGINE RUN COMPLETE | log=%s subject=%s ===",
        log_path,
        envelope.get("resolved_subject") if envelope else None,
    )

    if isinstance(envelope.get("mode"), str) and envelope["mode"] == "test":
        _print_test_footer()
    else:
        _print_production_summary(envelope)


if __name__ == "__main__":
    cli()
