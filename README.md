# Sovereign Knowledge Workbench

Sovereign Knowledge Workbench is a local-first orchestration layer for sorting,
extracting, reviewing, researching, and packaging personal knowledge files. It
coordinates probabilistic local models without giving them authority over the
filesystem or external recipients.

## Related repositories

- [Sovereign OS](https://github.com/domannand95-svg/sovereign-os) provides the
  deterministic identity, policy, capability, execution, and replay boundary.
- [Knowledge Infrastructure Bootstrap Kit](https://github.com/domannand95-svg/knowledge-infrastructure-bootstrap-kit)
  provides deterministic validation, normalization evidence, and provenance.

This repository is the orchestration and review layer between local files,
local models, BKI validation, and Sovereign authorization. It does not replace
either repository or acquire their authority.

The optional runtime plugin pack is pinned to the reviewed commit of
[Sovereign Workbench Plugins](https://github.com/domannand95-svg/sovereign-workbench-plugins).

## Responsibility boundary

| System | Responsibility |
| --- | --- |
| BKI | Deterministic document validation, normalization evidence, and provenance |
| Sovereign OS | Identity, policy, capability grants, effect authorization, and replay |
| Workbench | Read-only intake, extraction, classification candidates, review queues, routing candidates, and packages |
| Local model | Probabilistic classification and summary candidates only |
| Human operator | Final review, recipient choice, disclosure decision, and approval |

The governing rule is: **probabilistic intelligence; deterministic authority**.
Observations are not instructions, proposals are not authorization, and routing
candidates are not permission to disclose or send anything.

## Current capabilities

- recursively inventories regular files without following symbolic links;
- records SHA-256, size, type, and modification metadata;
- extracts bounded UTF-8 text and optionally PDF text;
- groups exact duplicates without deleting them;
- classifies content into modular review queues;
- flags common sensitive-data candidates;
- identifies explicit research-gap language;
- proposes recipient routes from a local configuration;
- optionally requests inert JSON classifications from an OpenAI-compatible local model;
- invokes BKI through its read-only `bki.validation.v1` CLI; and
- emits a canonical review package with a content digest.

File moves, deletion, package writes, publication, email, uploads, and other
external effects are not implemented as ambient actions. Package writing fails
closed unless a Sovereign authorizer command returns a bounded grant receipt.
External dispatch is deliberately not implemented in this baseline.

## Set up

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,pdf]"
.\.venv\Scripts\python.exe -m pytest
```

Install and use the governed plugin pack:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[plugins]"
.\.venv\Scripts\skw.exe plugin-list
.\.venv\Scripts\skw.exe plugin-run privacy.detect C:\path\to\document.md
```

Plugin results must be candidate-only, declare `authority: none`, and bind to
the exact source SHA-256 or the Workbench rejects them.

Resumable role-bounded batch processing:

```powershell
.\.venv\Scripts\skw.exe plugin-batch research.claims C:\path\to\archive `
  --state-db workbench-output\jobs.db `
  --include .md .txt `
  --limit 25 `
  --role researcher `
  --roles config\roles.v1.json
```

Role eligibility permits a tool request; it is not an execution capability.
Effectful plugins will additionally require a one-time Sovereign grant.

Completed results enter an append-only human review ledger:

```powershell
.\.venv\Scripts\skw.exe reviews-admit --state-db workbench-output\jobs.db
.\.venv\Scripts\skw.exe reviews-list --state-db workbench-output\jobs.db
.\.venv\Scripts\skw.exe reviews-decide CANDIDATE_ID needs_research `
  --reviewer operator --reason "Primary citation required" `
  --state-db workbench-output\jobs.db
```

A review approval is not filesystem, disclosure, or dispatch authorization.

Read-only deterministic analysis to standard output:

```powershell
.\.venv\Scripts\skw.exe scan C:\path\to\your\files `
  --routes config\routes.example.json
```

For a laptop-safe pilot over selected formats:

```powershell
.\.venv\Scripts\skw.exe scan C:\path\to\your\files `
  --include .md .txt .pdf `
  --max-files 25 `
  --max-file-mb 20
```

Review packages contain paths, hashes, classifications, findings, and proposals;
they deliberately exclude extracted source text.

Use an OpenAI-compatible local model such as Ollama:

```powershell
$env:SKW_MODEL_ENDPOINT = "http://127.0.0.1:11434/v1/chat/completions"
$env:SKW_MODEL_NAME = "qwen2.5-coder:7b"
.\.venv\Scripts\skw.exe scan C:\path\to\your\files --local-model
```

The model sees bounded extracted text and returns classification JSON. It does
not receive filesystem handles, policy keys, a capability registry, recipient
credentials, or dispatch tools.

Several models can run through a deterministic, concurrency-bounded schedule:

```powershell
.\.venv\Scripts\skw.exe models-run `
  --config config\models.ollama.example.json `
  --tasks workbench-output\model-tasks.json
```

Workers are selected by role and round robin. Failures are isolated and every
accepted result remains a `candidate` with `authority: none`.

## Authorization receipt verification

Effectful boundaries require `sovereign.authorization.receipt.v2`. The
Workbench independently verifies its Ed25519 signature against the pinned
`SKW_SOVEREIGN_VERIFYING_KEY`, checks the exact proposal digest, operation and
target, and consumes each grant once using `SKW_GRANT_LEDGER`. An authorizer's
self-reported verification flag is not trusted.

## BKI validation

```powershell
$env:SKW_PYTHON = "C:\path\to\knowledge-infrastructure-bootstrap-kit\.venv\Scripts\python.exe"
.\.venv\Scripts\skw.exe bki-validate `
  --source C:\path\to\source.md `
  --candidate C:\path\to\candidate.md `
  --bki-root C:\path\to\knowledge-infrastructure-bootstrap-kit
```

## Modules

| Module | Output |
| --- | --- |
| `intake` | Contained inventory and immutable byte identities |
| `extract` | Bounded local text extraction |
| `analysis` | Classification, privacy findings, and research gaps |
| `local_model` | Inert OpenAI-compatible classification candidates |
| `routing` | Configured recipient review queues |
| `adapters` | Fail-closed BKI and Sovereign process boundaries |
| `package` | Canonical review-only package and digest |

## Deliberate next gates

1. expose a stable Sovereign authorization/receipt command backed by the
   capability registry;
2. validate its signed grant and exact target binding in the workbench;
3. add OCR adapters running in isolated local processes;
4. add citation-resolution and source-quality modules;
5. add recipient-specific disclosure policies and redaction review; and
6. implement dispatch connectors only after separate capability consumption and
   explicit human confirmation for each destination.
