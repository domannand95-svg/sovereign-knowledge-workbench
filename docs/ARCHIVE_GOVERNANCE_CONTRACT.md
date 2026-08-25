# Archive Governance Contract

Status: **FROZEN CANDIDATE — implementation required before operational use**  
Contract identifier: `WORKBENCH-ARCHIVE-GOVERNANCE-001`  
Authority delta: **ZERO**

This contract separates intake, review evidence, research evidence, archive disposition,
and dataset admission. Observation, classification, validation, and evidence do not grant
filesystem, publication, training, or execution authority.

## 1. Intake roots

`00_Inbox` is the only default archive intake root.

The Workbench must not discover or treat any other directory as an intake root
automatically. A second root may participate only after an explicit append-only intake-root
admission records:

- a stable root identifier;
- its canonical absolute path;
- whether it is the default `00_Inbox` or an explicit additional root;
- its declared purpose;
- allowed file types;
- admission time; and
- the canonical admission-record hash.

At most one default root may exist. The basename of the default root must be exactly
`00_Inbox`. Root admission authorizes observation and review ingestion only. It does not
authorize moving, deleting, publishing, training on, or modifying files.

## 2. Returned research evidence

Returned Gemini or other provider research must enter through
`sovereign.workbench.research-evidence-return.v1`. A return must contain:

- the original research-ticket identifier and ticket hash;
- source identifiers and source hashes from the ticket;
- the exact research question;
- provider and model identity;
- completion time;
- claim-by-claim findings;
- citations and direct source locations;
- supporting evidence;
- contradictory evidence;
- uncertainty and unresolved questions; and
- a canonical return-package hash.

Import must fail closed when ticket identity, ticket hash, source identity, source hash,
question, schema, or return hash does not match. Successful import appends immutable evidence
only. Returned research has `authority = NONE` and cannot approve a file, change a review
decision, designate a canonical artifact, move a file, publish material, or enter a training
dataset automatically.

## 3. Review decisions, classifications, and relationships

The four human review decisions remain:

- `APPROVE`
- `NEEDS_RESEARCH`
- `QUARANTINE`
- `REJECT`

They must not be overloaded to encode every archive relationship.

| Archive meaning | Review decision | Separate append-only record |
| --- | --- | --- |
| Retain as useful | `APPROVE` | Topic, maturity, authority, and privacy classification |
| More evidence required | `NEEDS_RESEARCH` | Research ticket and later evidence-return relationship |
| Isolate from ordinary use | `QUARANTINE` | Quarantine reason |
| Not useful for the active archive | `REJECT` | Rejection reason |
| Historical archive | `APPROVE` | Maturity = `Archive` |
| Duplicate | No implied content decision | `DUPLICATE_OF` canonical source |
| Superseded draft | No implied deletion | `SUPERSEDED_BY` later source |
| Canonical version | Separate governed designation | `CANONICAL_OF` artifact family |
| Unresolved | No final decision | `NEEDS_REVIEW` state |

Duplicate, superseded, and canonical relationships are evidence-bearing relationships, not
deletion or authority instructions. Conflicting records are resolved by new append-only
records; existing records are never rewritten.

## 4. Dataset admission gate

A new adapter-training run is inadmissible until all of the following are true:

- at least **400 independently human-reviewed examples** exist;
- at least **300** are assigned `TRAIN_ELIGIBLE`;
- at least **100** are assigned `HELD_OUT_LOCKED`;
- every critical behavior category contains at least **25** examples;
- no single category exceeds 30 percent of the training set;
- all admitted examples have decision `APPROVE`;
- all admitted examples have complete source provenance and a verified source hash;
- no unresolved, research-only, quarantined, rejected, corrupt, privacy-ineligible, or
  provenance-incomplete example is admitted; and
- train and evaluation manifests are frozen and hashed before training begins.

The critical behavior categories are:

1. Sovereign OS terminology retention
2. Authority-boundary reasoning
3. Adversarial authority-escalation rejection
4. Schema and contract interpretation
5. Structured-output adherence
6. Ordinary coding ability
7. General reasoning preservation
8. Out-of-domain behavior
9. Hallucination handling
10. Regression behavior

These are minimum admission thresholds, not evidence that the resulting adapter will
generalize.

## 5. Immutable dataset allocation

Every eligible source family must be assigned exactly once, before dataset export, to one of:

- `TRAIN_ELIGIBLE`
- `HELD_OUT_LOCKED`
- `RESEARCH_ONLY`
- `EXCLUDED`

Allocation must record the source identifier, immutable source-family identifier, behavior
category, human reviewer, reason, time, and allocation-record hash.

All duplicates, revisions, extracted fragments, and directly derived records from one source
family must remain on the same side of the train/evaluation boundary. `HELD_OUT_LOCKED`
material must never be used for adapter training, prompt-template development, retry
selection, threshold tuning, or few-shot examples. Later review may append an exclusion but
must never move a held-out example into training.

Gemini research returns are `RESEARCH_ONLY` until separately reviewed and transformed into a
new provenance-bearing example. Provider output is never training-eligible merely because it
was returned successfully.

## 6. Required implementation gates

The current Workbench implementation predates this contract. Before dataset export or new
training is admitted, implementation must prove:

1. append-only intake-root admissions and the single-default-root constraint;
2. hash-bound research-ticket export and research-evidence-return import;
3. separate disposition/relationship records without deletion semantics;
4. immutable source-family dataset allocations;
5. rejection of every non-`APPROVE` training/evaluation candidate;
6. the 300/100 and per-category balance gates;
7. train/evaluation family disjointness;
8. non-overwriting, hash-bearing dataset manifests; and
9. unchanged authority and filesystem permissions.

Until those gates pass:

```text
ARCHIVE REVIEW: AVAILABLE
RESEARCH-TICKET EXPORT: DRAFT / NON-AUTHORITATIVE
RETURNED-RESEARCH INGESTION: NOT IMPLEMENTED
DATASET EXPORT: NOT ADMITTED FOR TRAINING
NEW ADAPTER TRAINING: BLOCKED
```

The governing invariant is:

> Evidence may inform classification, but cannot manufacture authority.
