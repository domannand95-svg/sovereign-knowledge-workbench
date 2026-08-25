from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from sovereign_workbench.extract import DOCX_MAX_MEMBERS, extract_text


def write_docx(path: Path, body: bytes, *, compression: int = ZIP_DEFLATED) -> None:
    with ZipFile(path, "w", compression=compression) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", body)


def test_docx_extracts_paragraph_text_with_bound(tmp_path: Path):
    document = b'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>Research evidence</w:t></w:r></w:p>
      <w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p></w:body></w:document>'''
    path = tmp_path / "sample.docx"
    write_docx(path, document)
    assert extract_text(path) == ("Research evidence\nSecond paragraph", "extracted")
    assert extract_text(path, max_chars=8) == ("Research", "extracted")


def test_invalid_or_missing_docx_fails_closed(tmp_path: Path):
    invalid = tmp_path / "invalid.docx"
    invalid.write_bytes(b"not-a-zip")
    assert extract_text(invalid) == (None, "docx_invalid_archive")
    missing = tmp_path / "missing.docx"
    with ZipFile(missing, "w") as archive:
        archive.writestr("other.xml", "<other/>")
    assert extract_text(missing) == (None, "docx_document_missing")


def test_malformed_docx_xml_fails_closed(tmp_path: Path):
    path = tmp_path / "malformed.docx"
    write_docx(path, b"<w:document")
    assert extract_text(path) == (None, "docx_xml_invalid")


def test_docx_dtd_and_entities_are_rejected_before_parsing(tmp_path: Path):
    path = tmp_path / "entity.docx"
    write_docx(path, b'<!DOCTYPE x [<!ENTITY a "expanded">]><x>&a;</x>')
    assert extract_text(path) == (None, "docx_xml_unsafe")


def test_docx_member_count_is_bounded_before_extraction(tmp_path: Path):
    path = tmp_path / "many-members.docx"
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", "<document/>")
        for index in range(DOCX_MAX_MEMBERS):
            archive.writestr(f"padding/{index}", b"")
    assert extract_text(path) == (None, "docx_limits_exceeded")
