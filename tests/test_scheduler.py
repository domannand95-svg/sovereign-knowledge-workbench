import json

import pytest

from sovereign_workbench.scheduler import (ModelWorker, SchedulerError, connect_schedule,
    enqueue_schedule, plan, run_pending_schedule, run_schedule, schedule_counts)


WORKERS = [
    ModelWorker("a", "model-a", ("review",), "local", 10),
    ModelWorker("b", "model-b", ("review",), "local", 10),
]


def test_schedule_is_deterministic_and_round_robin():
    tasks = [{"task_id": "2", "role": "review", "prompt": "two"}, {"task_id": "1", "role": "review", "prompt": "one"}]
    assert [(task["task_id"], worker.worker_id) for task, worker in plan(tasks, WORKERS)] == [("1", "a"), ("2", "b")]


def test_schedule_isolates_failure_and_preserves_no_authority():
    tasks = [{"task_id": "1", "role": "review", "prompt": "one"}, {"task_id": "2", "role": "review", "prompt": "two"}]
    def invoke(task, worker):
        if task["task_id"] == "1":
            raise RuntimeError("offline")
        return {"task_id": task["task_id"], "status": "candidate", "authority": "none", "proposal": {}}
    results = run_schedule(tasks, WORKERS, max_concurrency=2, invoke=invoke)
    assert results[0]["status"] == "failed"
    assert results[1]["authority"] == "none"


def test_schedule_rejects_missing_role():
    with pytest.raises(SchedulerError, match="No worker"):
        plan([{"task_id": "1", "role": "write", "prompt": "x"}], WORKERS)


def test_schedule_rejects_duplicate_task_identity():
    with pytest.raises(SchedulerError, match="Duplicate"):
        plan([{"task_id":"1","role":"review","prompt":"a"},
              {"task_id":"1","role":"review","prompt":"b"}], WORKERS)


def test_durable_schedule_recovers_and_rejects_identity_reuse(tmp_path):
    tasks = [{"task_id":"1","role":"review","prompt":"one"}]
    with connect_schedule(tmp_path / "models.db") as database:
        assert enqueue_schedule(database, tasks, WORKERS) == 1
        database.execute("UPDATE model_jobs SET status='running'"); database.commit()
        result = run_pending_schedule(database, max_concurrency=1,
            invoke=lambda task, worker: {"task_id":task["task_id"], "worker_id":worker.worker_id,
                "model":worker.model, "status":"candidate", "authority":"none", "proposal":{}})
        assert result == {"completed":1,"failed":0}
        assert schedule_counts(database)["completed"] == 1
        with pytest.raises(SchedulerError, match="different content"):
            enqueue_schedule(database, [{"task_id":"1","role":"review","prompt":"changed"}], WORKERS)
