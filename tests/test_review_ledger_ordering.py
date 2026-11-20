"""Decisions made within one clock tick must keep their order.

Found as an intermittent failure of test_reviews.py on Windows (2026-11-20): two decisions
recorded in quick succession could share a timestamp, and "latest decision" and the chain
check then fell back to comparing decision-id hashes. The wrong decision could be shown as
current, and a valid ledger could be reported as failing verification.
"""
import pytest

import sovereign_workbench.reviews as reviews
from sovereign_workbench.reviews import decide, list_candidates, verify_decisions

from test_reviews import _candidate

SEQUENCE = ["needs_research", "quarantined", "rejected", "approved", "needs_research", "approved"]


def test_same_timestamp_decisions_keep_their_order(tmp_path, monkeypatch):
    database, candidate = _candidate(tmp_path)
    monkeypatch.setattr(reviews, "_now", lambda: "2026-11-20T00:00:00.000000Z")
    ids = [decide(database, candidate["candidate_id"], decision, "operator", f"step {index}")
           for index, decision in enumerate(SEQUENCE)]
    latest = list_candidates(database)[0]
    assert (latest["decision"], latest["reason"]) == (SEQUENCE[-1], f"step {len(SEQUENCE) - 1}")
    links = dict(database.execute(
        "SELECT decision_id,previous_decision_id FROM review_decisions").fetchall())
    assert [links[i] for i in ids] == [None, *ids[:-1]]  # one linear chain
    assert verify_decisions(database) == {"valid": True, "candidates": 1, "decisions": len(SEQUENCE)}
    database.close()


def test_whole_second_timestamp_does_not_sort_after_fractional_ones(tmp_path, monkeypatch):
    database, candidate = _candidate(tmp_path)
    # isoformat() drops ".000000"; as text "05Z" sorts after "05.5...Z".
    stamps = iter(["2026-11-20T00:00:05Z", "2026-11-20T00:00:05.500000Z"])
    monkeypatch.setattr(reviews, "_now", lambda: next(stamps))
    decide(database, candidate["candidate_id"], "rejected", "operator", "first")
    decide(database, candidate["candidate_id"], "approved", "operator", "second")
    assert list_candidates(database)[0]["reason"] == "second"
    assert verify_decisions(database)["decisions"] == 2
    database.close()


def test_timestamps_always_carry_microseconds():
    assert len(reviews._now()) == len("2026-11-20T00:00:00.000000Z")


def test_a_forked_chain_still_fails_verification(tmp_path, monkeypatch):
    database, candidate = _candidate(tmp_path)
    first = decide(database, candidate["candidate_id"], "rejected", "operator", "first")
    decide(database, candidate["candidate_id"], "approved", "operator", "second")
    # Forge a second successor of the first decision, with a self-consistent hash.
    decided_at = "2026-11-20T00:00:09.000000Z"
    forged = reviews._decision_hash(candidate["candidate_id"], "approved", "operator", "forged",
                                    decided_at, first)
    database.execute(
        "INSERT INTO review_decisions(decision_id,candidate_id,decision,reviewer,reason,decided_at,"
        "previous_decision_id,decision_hash) VALUES(?,?,?,?,?,?,?,?)",
        (forged, candidate["candidate_id"], "approved", "operator", "forged", decided_at, first, forged))
    with pytest.raises(ValueError, match="chain verification failed"):
        verify_decisions(database)
    database.close()
