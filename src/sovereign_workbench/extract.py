from __future__ import annotations

from pathlib import Path


TEXT_SUFFIXES = {
    ".txt", ".md", ".rst", ".csv", ".tsv", ".json", ".jsonl", ".yaml",
    ".yml", ".toml", ".xml", ".html", ".htm", ".py", ".rs", ".js", ".ts",
}


def extract_text(path: Path, *, max_chars: int = 200_000) -> tuple[str | None, str]:
    if path.suffix.casefold() in TEXT_SUFFIXES:
        try:
            return path.read_text(encoding="utf-8", errors="strict")[:max_chars], "extracted"
        except UnicodeDecodeError:
            return None, "invalid_utf8"
        except OSError:
            return None, "read_error"
    if path.suffix.casefold() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return None, "pdf_dependency_missing"
        try:
            text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
            return text[:max_chars], "extracted"
        except Exception:
            return None, "pdf_extraction_failed"
    return None, "unsupported_type"
