import json
from pathlib import Path

import pytest

from sovereign_workbench.corpus_hardening import (
    apply_contamination_exclusions, export_topic_research_tickets,
    generate_topic_research_tickets,
)
from sovereign_workbench.intake import scan_files
from sovereign_workbench.review_batches import admit, connect, create_next, register_intake_root
from sovereign_workbench.section_review import (
    build_section_proposals, decide_document_map, document_exception_maps,
)


def _database(tmp_path: Path):
    root=tmp_path/"archive"; root.mkdir()
    (root/"mixed.txt").write_text(
        "GOVERNANCE:\nPolicy quorum authority execution capability authorization.\n"
        "PERSONAL MATERIAL:\nO.R.A. Ordo Rara Avis and 432 Hz solar-vortex fractal resonance.\n"
        "SPECULATIVE COSMOLOGY:\nArbor Inversa proposes a holographic universe and simulation cosmology.\n"
        "ARCHIVE LABEL:\nMaterial submitted to the Arbor Inversa Technical Archive.\n"
        "ARBITRARY DESIGN:\nUFRF constrains a golden ratio reactor and 4.32 MHz clock using Fibonacci-Sexagesimal timing.\n"
        "ENERGY:\nPlasma reactor power thermal energy confinement measurement.\n"
        "CONTACTS:\nResearch collaborators and archival storage requirements.",encoding="utf-8")
    database=connect(tmp_path/"state.db"); register_intake_root(database,root,purpose="test",allowed_suffixes={".txt"}); admit(database,root,scan_files(root)); create_next(database); build_section_proposals(database)
    value=document_exception_maps(database)[0]
    decide_document_map(database,value["source_id"],value["source_sha256"],value["map_sha256"],"MAP_ACCEPTED","Reviewer","Topic split accepted")
    return database


def test_contamination_exclusions_are_precise_immutable_and_block_tickets(tmp_path: Path):
    with _database(tmp_path) as database:
        result=apply_contamination_exclusions(database,reviewer="Reviewer")
        assert result["inserted"] >= 3
        excluded_headings={row[0] for row in database.execute(
            "SELECT DISTINCT x.heading FROM archive_section_exclusions e JOIN archive_document_sections x ON x.section_id=e.section_id")}
        assert excluded_headings == {"PERSONAL MATERIAL", "SPECULATIVE COSMOLOGY",
                                     "ARCHIVE LABEL", "ARBITRARY DESIGN"}
        assert apply_contamination_exclusions(database,reviewer="Reviewer")["inserted"]==0
        with pytest.raises(Exception,match="immutable"):
            database.execute("DELETE FROM archive_section_exclusions")
        tickets=generate_topic_research_tickets(database,max_sections=10)
        assert tickets["accepted_sources"]==1 and tickets["excluded_sections"]==4
        payload=" ".join(row[0] for row in database.execute("SELECT question FROM archive_section_research_tickets"))
        assert "PERSONAL MATERIAL" not in payload
        assert "SPECULATIVE COSMOLOGY" not in payload
        assert "ARCHIVE LABEL" not in payload
        assert "ARBITRARY DESIGN" not in payload
        assert "GOVERNANCE" in payload and "ENERGY" in payload


def test_topic_ticket_export_is_separate_hash_bound_and_non_overwriting(tmp_path: Path):
    output=tmp_path/"tickets.jsonl"
    with _database(tmp_path) as database:
        apply_contamination_exclusions(database,reviewer="Reviewer")
        generate_topic_research_tickets(database,max_sections=10)
        count=export_topic_research_tickets(database,output)
        rows=[json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        assert count==len(rows)>1
        assert all(row["authority"]=="NONE" and row["status"]=="DRAFT" for row in rows)
        assert len({row["proposed_topic"] for row in rows})>1
        with pytest.raises(ValueError,match="already exists"):
            export_topic_research_tickets(database,output)


def test_export_omits_draft_tickets_invalidated_by_later_exclusions(tmp_path: Path):
    output = tmp_path / "filtered.jsonl"
    with _database(tmp_path) as database:
        generate_topic_research_tickets(database, max_sections=10)
        apply_contamination_exclusions(database, reviewer="Reviewer")
        count = export_topic_research_tickets(database, output)
        excluded = {row[0] for row in database.execute(
            "SELECT DISTINCT section_id FROM archive_section_exclusions")}
        rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        assert count == len(rows)
        assert all(not (set(row["section_ids"]) & excluded) for row in rows)


def test_latex_frequency_and_broader_fringe_engineering_are_excluded(tmp_path: Path):
    root = tmp_path / "archive"; root.mkdir()
    (root / "fringe.txt").write_text(
        "LATEX CLOCK:\nSteps are phase-locked to the 432\\text{ Hz} harmonic baseline.\n"
        "VORTEX CODE:\nRodin vortex weights drive the coherence score.\n"
        "COSMOLOGY:\nTime is a fractal waveform governed by retrocausality and an Omega Point.\n"
        "RITUAL UI:\nThe Vajra Bridge drives a Zodiac Wheel on the ritual dashboard.\n",
        encoding="utf-8")
    with connect(tmp_path / "state.db") as database:
        register_intake_root(database, root, purpose="test", allowed_suffixes={".txt"})
        admit(database, root, scan_files(root)); create_next(database); build_section_proposals(database)
        result = apply_contamination_exclusions(database, reviewer="Reviewer")
        assert result["FREQUENCY_432"] == 1
        assert result["RODIN_VORTEX_CONTAMINATION"] == 1
        assert result["FRACTAL_COSMOLOGY_CONTAMINATION"] == 1
        assert result["RITUAL_COSMIC_TECH_CONTAMINATION"] == 1
