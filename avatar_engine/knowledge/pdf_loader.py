# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path


def _sorted_pdfs_under(pdf_directory: Path) -> list[Path]:
    if not pdf_directory.exists():
        return []
    return sorted(
        pdf_directory.rglob("*.pdf"),
        key=lambda p: str(p.relative_to(pdf_directory)).lower(),
    )


def list_pdf_relative_paths(pdf_directory: Path) -> list[str]:
    """Relative POSIX paths under ``pdf_directory`` for diagnostics / knowledge tests."""
    base = pdf_directory
    paths: list[str] = []
    for pdf in _sorted_pdfs_under(base):
        rel = pdf.relative_to(base).as_posix()
        paths.append(rel if rel.lower().endswith(".pdf") else f"{rel}.pdf")
    return paths


def _read_pdf_text(path: Path, max_chars: int) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    text_parts: list[str] = []
    remaining = max_chars
    for page in reader.pages:
        if remaining <= 0:
            break
        try:
            block = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            block = ""
        excerpt = block[:remaining]
        text_parts.append(excerpt.strip())
        remaining -= len(excerpt)
    return "\n\n".join(t for t in text_parts if t)


def load_digital_product_corpus(pdf_directory: Path, *, chunk_char_limit: int) -> dict[str, str]:
    """Return `{relative/path.pdf: excerpted_text}` for every PDF discovered."""
    corpus: dict[str, str] = {}
    pdfs = _sorted_pdfs_under(pdf_directory)
    if not pdfs:
        return corpus
    per_file_budget = max(chunk_char_limit // max(len(pdfs), 1), 4000)
    directory = pdf_directory
    for pdf in pdfs:
        rel_path = pdf.relative_to(directory).as_posix()
        label = rel_path if rel_path.lower().endswith(".pdf") else f"{rel_path}.pdf"
        try:
            corpus[label] = _read_pdf_text(pdf, per_file_budget)
        except Exception as exc:  # noqa: BLE001
            corpus[label] = f"[Could not read PDF `{label}`: {exc}]"
    return corpus


def corpus_to_prompt_context(corpus: dict[str, str]) -> str:
    blocks: list[str] = []
    for name, body in corpus.items():
        blocks.append(f"### SOURCE FILE: {name}\n{body}")
    return "\n\n".join(blocks)
