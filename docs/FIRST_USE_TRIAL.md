# Supervised first-use trial

A short, repeatable procedure for a first supervised trial of the archive review
workflow on a **synthetic** folder. It checks that you can import, review, search and
find every document that needs attention. It does not move, copy, export or disclose
any document, and a review decision is a record, not permission to do any of those.

Run the commands from the repository folder in PowerShell. The examples use
`C:\skw-trial` for everything; change it if you like.

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
text shown below the summary. Files that changed or moved since import cannot be decided;
use **Skip for now**. Alternatively, export a batch to CSV with `archive-batch-export`,
fill in the decision columns, and load it with `archive-review-import`.

## 4. Find documents that need attention

| What | Where it shows |
|---|---|
| **Excluded** (larger than `--max-file-mb`, never imported) | `excluded` in the `archive-batch-create` output and in `archive-batch-status`, plus a warning |
| **Changed, moved or missing** since import | `unverified_sources` in any `archive-search` result (the list is always complete); such files are blocked on the review screen |
| **Changed before batching** (after an interrupted import) | `stale_pending` in the `archive-batch-create` output, plus a warning. They stay counted as `pending` |
| **No readable text** (empty, scanned, unreadable format) | `not_searchable_sources` in `archive-search`; they can still be reviewed after you open the file itself |
| **Model fell back** (only with `--local-model`) | `model_failures` in the import output and a warning; each affected file says so in its proposal reason |
| **Not yet reviewed** | `archive-batch-status`: `pending` (not batched yet) and `batched` (batched, no decision). `reviewed` and `quarantined` have a recorded human decision |

```powershell
.\.venv\Scripts\skw.exe archive-batch-status --state-db C:\skw-trial\review.db
```

```powershell
.\.venv\Scripts\skw.exe archive-search "any word" --state-db C:\skw-trial\review.db
```

## 5. What a successful trial looks like

- Every file in the folder is either in a batch or listed as excluded.
- After an interrupted import and a re-run, nothing is duplicated or lost.
- Edited, moved and deleted files show up as unverified, and none of their current text
  is shown.
- `archive-batch-status` counts match what you decided.

Note anything confusing or wrong, with the command and its output, but **no real
document content**.
