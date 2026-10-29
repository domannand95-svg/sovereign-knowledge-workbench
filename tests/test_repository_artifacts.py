from pathlib import Path

import pytest

from sovereign_workbench.intake import scan_files
from sovereign_workbench.repository_artifacts import route_repository_artifact
from sovereign_workbench.review_batches import admit, connect, create_next, register_intake_root
from sovereign_workbench.section_review import build_section_proposals, document_exception_maps, exception_queue


def test_repository_route_is_hash_bound_excluded_immutable_and_does_not_move(tmp_path: Path):
    root = tmp_path / "archive"; root.mkdir()
    source = root / "roadmap.txt"; source.write_text("version 1", encoding="utf-8")
    canonical = tmp_path / "repo" / "roadmap.md"; canonical.parent.mkdir()
    canonical.write_text("version 2", encoding="utf-8")
    with connect(tmp_path / "state.db") as database:
        register_intake_root(database, root, purpose="test", allowed_suffixes={".txt"})
        admit(database, root, scan_files(root)); create_next(database); build_section_proposals(database)
        row = database.execute(
            "SELECT source_id,source_sha256 FROM review_sources"
        ).fetchone()
        with pytest.raises(ValueError, match="hash mismatch"):
            route_repository_artifact(
                database, source_id=row[0], source_sha256="0" * 64,
                repository_name="example", canonical_path=canonical,
                relationship="SUPERSEDED_REPOSITORY_COPY", reviewer="Reviewer", reason="newer copy")
        route_id = route_repository_artifact(
            database, source_id=row[0], source_sha256=row[1],
            repository_name="example", canonical_path=canonical,
            relationship="SUPERSEDED_REPOSITORY_COPY", reviewer="Reviewer", reason="newer copy")
        assert route_id.startswith("RAR-")
        assert database.execute(
            "SELECT dataset_disposition,movement_authority,authority "
            "FROM archive_repository_artifact_routes"
        ).fetchone() == ("EXCLUDED", "NOT_AUTHORIZED", "NONE")
        maps = document_exception_maps(database)
        assert maps[0]["artifact_role"] == "REPOSITORY_ARTIFACT"
        assert exception_queue(database) == []
        with pytest.raises(Exception, match="immutable"):
            database.execute("DELETE FROM archive_repository_artifact_routes")
    assert source.exists() and canonical.exists()
