from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


TEXT_SUFFIXES = {
    ".txt", ".md", ".rst", ".csv", ".tsv", ".json", ".jsonl", ".yaml",
    ".yml", ".toml", ".xml", ".html", ".htm", ".py", ".rs", ".js", ".ts",
}

DOCX_DOCUMENT_PATH = "word/document.xml"
DOCX_MAX_MEMBERS = 2_048
DOCX_MAX_XML_BYTES = 16 * 1024 * 1024
DOCX_MAX_COMPRESSION_RATIO = 200
WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _extract_docx(path: Path, max_chars: int) -> tuple[str | None, str]:
    try:
        with ZipFile(path) as archive:
            if len(archive.infolist()) > DOCX_MAX_MEMBERS:
                return None, "docx_limits_exceeded"
            try:
                document = archive.getinfo(DOCX_DOCUMENT_PATH)
            except KeyError:
                return None, "docx_document_missing"
            compressed_size = max(document.compress_size, 1)
            if (document.flag_bits & 0x1 or document.file_size > DOCX_MAX_XML_BYTES
                    or document.file_size / compressed_size > DOCX_MAX_COMPRESSION_RATIO):
                return None, "docx_limits_exceeded"
            with archive.open(document) as source:
                payload = source.read(DOCX_MAX_XML_BYTES + 1)
            if len(payload) > DOCX_MAX_XML_BYTES:
                return None, "docx_limits_exceeded"
    except (BadZipFile, OSError):
        return None, "docx_invalid_archive"

    lowered_payload = payload.lower()
    if b"<!doctype" in lowered_payload or b"<!entity" in lowered_payload:
        return None, "docx_xml_unsafe"
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return None, "docx_xml_invalid"

    paragraphs: list[str] = []
    paragraph_tag = f"{{{WORD_NAMESPACE}}}p"
    text_tag = f"{{{WORD_NAMESPACE}}}t"
    for paragraph in root.iter(paragraph_tag):
        text = "".join(node.text or "" for node in paragraph.iter(text_tag))
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)[:max_chars], "extracted"


def extract_text(path: Path, *, max_chars: int = 200_000) -> tuple[str | None, str]:
    if path.suffix.casefold() in TEXT_SUFFIXES:
        try:
            return path.read_text(encoding="utf-8", errors="strict")[:max_chars], "extracted"
        except UnicodeDecodeError:
            return None, "invalid_utf8"
        except OSError:
            return None, "read_error"
    if path.suffix.casefold() == ".docx":
        return _extract_docx(path, max_chars)
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
