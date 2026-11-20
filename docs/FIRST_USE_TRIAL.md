# Supervised first-use trial

A short, repeatable procedure for a first supervised trial of the archive review
workflow on a **synthetic** folder. It checks that you can import, review, search and
find every document that needs attention. It does not move, copy, export or disclose
any document, and a review decision is a record, not permission to do any of those.

Run the commands from the repository folder in PowerShell. The examples use
`C:\skw-trial` for everything; change it if you like.

Use only the commands below. The family, section and dataset commands
(`archive-family-*`, `archive-section-*`) and the `research.claims` plugin still store
document excerpts and are not part of this trial. `archive-batch-status` reports
`stored_document_text`; it should say `"contains_document_text": false`.

## 1. Make a synthetic folder

Use invented content only. A quick way is the end-to-end script, which builds a mixed
folder (text, Markdown, DOCX, PDF, duplicates, an empty file, a non-UTF-8 file, a file
over 20 MB, non-Latin names) and runs the whole workflow against it unattended:

```powershell
.\.venv\Scripts\python.exe scripts\e2e_synthetic_archive.py --files 300 --workdir C:\skw-trial\auto
```

It prints a JSON report; every check should say `"ok": true`. The script only ever
deletes a work folder it created itself. For the hands-on trial below, copy its
`archive` folder to `C:\skw-trial\docs` (a few files in it were edited, deleted or
renamed by the run, which is useful practice), or make your own small folder.

## 2. Register the folder and import

```powershell
.\.venv\Scripts\skw.exe archive-intake-register C:\skw-trial\docs --state-db C:\skw-trial\review.db --purpose "first-use trial" --include .txt .md .docx .pdf
```

```powershell
.\.venv\Scripts\skw.exe archive-batch-create C:\skw-trial\docs --state-db C:\skw-trial\review.db --include .txt .md .docx .pdf --limit 100
```

Repeat the import until its output shows `"pending": 0`. Each run makes one batch of up
to `--limit` files. It is safe to stop an import part-way (close the window or press
Ctrl+C) and run it again.

## 3. Review

```powershell
.\scripts\start-local-review.ps1 -StateDb C:\skw-trial\review.db -Reviewer "Your name"
```

The browser screen shows one file at a time. Decide only after reading the document
text shown below the summary. The choices are APPROVE, NEEDS RESEARCH, QUARANTINE and
REJECT; each needs a short note. **Skip for now** records nothing. Files that changed or
moved since import are set aside automatically and listed at the top of the screen;
nothing is recorded for them. Alternatively, export a batch to CSV with `archive-batch-export`,
fill in the decision columns, and load it with `archive-review-import`.

## 4. Find documents that need attention

| What | Where it shows |
|---|---|
| **Excluded** (larger than `--max-file-mb`, never imported) | `excluded` in the `archive-batch-create` output and in `archive-batch-status`, plus a warning |
| **Changed, moved or missing** since import | The "Not searched" list of any `archive-search` (always complete; a moved file shows where the same content now is, when known); the review screen lists them as set aside |
| **Changed before batching** (after an interrupted import) | `archive-batch-status` under "Waiting to be batched", each with its state and an explanation; also a warning from `archive-batch-create` |
| **No readable text** (empty, scanned, unreadable format) | `not_searchable_sources` in `archive-search`; they can still be reviewed after you open the file itself |
| **Model fell back** (only with `--local-model`) | `model_failures` in the import output and a warning; each affected file says so in its proposal reason |
| **Not yet reviewed** | `archive-batch-status`: "Waiting to be batched" and "Batched (waiting for a decision)". "Reviewed" and "Quarantined" have a recorded human decision |

```powershell
.\.venv\Scripts\skw.exe archive-batch-status --state-db C:\skw-trial\review.db
```

```powershell
.\.venv\Scripts\skw.exe archive-search "any word" --state-db C:\skw-trial\review.db
```

Both commands print plain text in a terminal; add `--format json` for the full details.

## 5. What a successful trial looks like

- Every file in the folder is either in a batch or listed as excluded.
- After an interrupted import and a re-run, nothing is duplicated or lost.
- Edited, moved and deleted files show up as unverified, and none of their current text
  is shown.
- `archive-batch-status` counts match what you decided.

Note anything confusing or wrong, with the command and its output, but **no real
document content**.
