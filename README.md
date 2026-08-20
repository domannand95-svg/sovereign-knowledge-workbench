# Sovereign Knowledge Workbench

Sovereign Knowledge Workbench is a local-first orchestration layer for sorting,
extracting, reviewing, researching, and packaging personal knowledge files. It
coordinates probabilistic local models without giving them authority over the
filesystem or external recipients.

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

Read-only deterministic analysis to standard output:

```powershell
.\.venv\Scripts\skw.exe scan C:\path\to\your\files `
  --routes config\routes.example.json
```

Use an OpenAI-compatible local model such as Ollama:

```powershell
$env:SKW_MODEL_ENDPOINT = "http://127.0.0.1:11434/v1/chat/completions"
$env:SKW_MODEL_NAME = "qwen2.5-coder:7b"
.\.venv\Scripts\skw.exe scan C:\path\to\your\files --local-model
```

The model sees bounded extracted text and returns classification JSON. It does
not receive filesystem handles, policy keys, a capability registry, recipient
credentials, or dispatch tools.

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
