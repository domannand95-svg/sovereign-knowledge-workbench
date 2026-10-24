# Local dispatch and explicit staging retries — proposed contract

This candidate supports one cooperating dispatcher per canonical file-backed local
SQLite database, with bounded concurrent model jobs inside that dispatcher. A
second dispatcher/recovery call fails visibly while the owner remains alive. Stop
older runner versions before migration. Keep the persistent `.runner.lock` file;
do not unlink/replace it while dispatchers can be active. Mixed versions, hard-link
aliases, multi-host/network filesystem locking and fork-inherited lock lifetime
are not qualified. A row claim or OS lock does not provide at-most-once external
effects. Interrupted candidate jobs retain the existing recovery behavior; failed
queue rows are not automatically retried. The new linked attempts below concern
staging, not a new external-provider retry protocol.

Staging execution and `stage-recover` still require quiesced staging writers.
Conditional updates guard individual transitions, not the whole filesystem
transaction. Publication is allowed only after an exact conditional rollback-intent
update and a successfully returned SQLite commit. Failure does not clear prior
intent. An existing staging attempt is never overwritten to retry it.

## Linked staging proposal

`stage-retry-plan PARENT_PLAN_ID --state-db FILE --reason TEXT` records a new
non-authoritative plan and an immutable parent link, then prints that plan. It does
not invoke an authorizer or copy/delete files. Parent must be failed. Each parent
has at most one linked child; inspect that child rather than create parallel
siblings. A failed child may have its own linked child. Preparing a plan is not
approval to execute it, even if human review requested a retry.

A v2 plan binds `retry_of_plan_id`, `parent_snapshot_sha256` and a fresh
`attempt_nonce` into its version-specific plan hash. Its destination is a separate
private directory based on that new hash. The original source/root lineage and
full parent-row snapshot are retained. Link records cannot be updated/deleted via
normal SQL. The failed parent, receipts, rollback intent and any partial copy stay
untouched. This is local staging provenance, not a new global authority ledger.

An unused retry may execute through `stage-execute --plan FILE --state-db FILE`,
which preflights before requesting fresh authorization and rechecks before claiming.
The existing independent signature verification, proposal binding and atomic
one-time grant/nonce consumption remain mandatory at the caller boundary. The new
plan ID is bound as `report_sha256`; a parent's receipt/target is not reusable for
its child. Missing/denied/invalid authorization produces no new copy. A linked
proposal grants no authority and there is no fallback to an unlinked retry.

## Uncertain effects stay blocked

A new nullable `publication_state` column deliberately leaves migrated attempts
NULL (unknown). Newly claimed attempts start `not_attempted`; the write-ahead intent
transaction moves them to `publication_pending` before rename, and a guarded final
commit records `published`. `not_attempted` means this implementation has not yet
initiated publication for that attempt under the trusted local DB/quiescence
contract. It is not proof of absence of arbitrary external effects.

Retry execution additionally requires an unchanged failed parent snapshot,
`publication_state=not_attempted`, no parent rollback intent and no existing parent
destination/symlink. Partial files are preserved. Legacy/unknown or
publication-pending/published parents stay blocked, including when their target is
absent. Absence alone cannot reconcile a crash. A proposal may be retained for
review while blocked, but no command here can mark it safe by assertion. Repair or
reconciliation of these cases needs a separate reviewed contract and fresh
applicable authority; manual database editing is not a supported repair path.

Recovery only observes prior staging/rollback effects. It never publishes a partial,
repeats a delete or grants permission to retry. Stored receipt shape is not signature
verification. Callers must retain verified receipt and grant-ledger provenance.
Original v1 plans and ordinary copy/rollback paths remain readable/executable for
unused attempts. Existing attempts stay preserved; no implicit legacy phase is
invented during migration.

## Qualification

The native standard-library contract test can run without pytest once existing
runtime dependencies are present:

```sh
PYTHONPATH=src python -B -m unittest discover -s tests -p test_staging_retry_contract.py -v
```

Use the repository's normal `python -m pytest` suite with its declared dev/plugin
extras for full integration qualification. Windows and Python 3.11 need their own
runs; Linux Python 3.12 results do not qualify them. Synthetic signed-receipt tests
use ephemeral keys and a mocked authorizer subprocess, never a live policy service.
Power loss, directory-entry fsync, malicious filesystem races and actual external
providers are not certified by these tests. This is a review candidate; no deployment
or activation follows merely from preparing or testing it.
