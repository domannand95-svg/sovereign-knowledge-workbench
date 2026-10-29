from pathlib import Path

import pytest

from sovereign_workbench.concept_directives import apply_concept_directives
from sovereign_workbench.intake import scan_files
from sovereign_workbench.review_batches import admit, connect, create_next, register_intake_root
from sovereign_workbench.section_review import build_section_proposals


def test_concept_directives_are_distinct_immutable_and_non_authoritative(tmp_path: Path):
    root = tmp_path / "archive"; root.mkdir()
    (root / "concepts.txt").write_text(
        "SPECULATIVE:\n15.3 MHz neural bus.\n"
        "LAB NETWORK:\nIndra's Net 6G mesh networking.\n"
        "ASSESS:\nCognitive negentropy and information-theoretic thermodynamics.\n"
        "SUPERSEDED:\nMolecular Mint elemental transmutation.\n"
        "ISOTOPES:\nParticle accelerator isotope production.\n"
        "FUTURE COMPUTE:\nTCF-01A asynchronous neuromorphic ternary triadic logic.\n"
        "MPCVD:\nMicrowave Plasma Chemical Vapor Deposition is a retained research avenue.\n"
        "MEDICAL RESEARCH:\nAtmospheric CNS stabilization requires clinical evidence.",
        encoding="utf-8")
    with connect(tmp_path / "state.db") as database:
        register_intake_root(database, root, purpose="test", allowed_suffixes={".txt"})
        admit(database, root, scan_files(root)); create_next(database); build_section_proposals(database)
        result = apply_concept_directives(database, reviewer="Reviewer")
        assert result["inserted"] == 8
        dispositions = {row[0] for row in database.execute(
            "SELECT disposition FROM archive_section_directives")}
        assert dispositions == {"SPECULATIVE_HOLD", "CLOSED_LAB_RESEARCH", "ASSESS_CONTAMINATION",
                                "SUPERSEDED_NONVIABLE", "RESEARCH_REQUIRED",
                                "FUTURE_FUNDING_RESEARCH"}
        assert database.execute(
            "SELECT COUNT(*) FROM archive_section_directives WHERE authority='NONE'"
        ).fetchone()[0] == 8
        assert database.execute(
            "SELECT COUNT(*) FROM archive_section_directives "
            "WHERE concept_code='CNS_STABILIZATION_RESEARCH' AND disposition='RESEARCH_REQUIRED'"
        ).fetchone()[0] == 1
        assert apply_concept_directives(database, reviewer="Reviewer")["inserted"] == 0
        with pytest.raises(Exception, match="immutable"):
            database.execute("DELETE FROM archive_section_directives")
