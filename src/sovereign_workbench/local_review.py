from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .review_batches import REVIEW_COLUMNS, connect, record_review_decision


@dataclass(frozen=True)
class ReviewItem:
    batch_id: str
    source_id: str
    root: str
    source_path: str
    source_sha256: str
    modified_ns: int
    extraction_status: str
    model_summary: str
    proposed_topic: str
    proposed_maturity: str
    proposed_authority: str
    proposal_reason: str
    classification_confidence: float
    review_route: str
    route_reasons: str


def _hash_stable_file(path: Path, expected_size: int) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("The source is missing, non-regular, or symbolic; review acceptance is blocked")
    before = path.stat()
    if before.st_size != expected_size:
        raise ValueError("The source size changed after admission; review acceptance is blocked")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("The source changed while it was being verified")
    return digest.hexdigest()


def propose_research_question(item: ReviewItem) -> str:
    subject = item.source_path.rsplit(".", 1)[0]
    return (
        f"Which claims in {subject!r} are supported by primary evidence, which remain speculative, "
        "and what module and maturity classification is justified after contradictory evidence "
        "and uncertainty are recorded?"
    )


class ReviewController:
    def __init__(self, database: sqlite3.Connection, reviewer: str):
        if not reviewer.strip():
            raise ValueError("A human reviewer name is required")
        self.database = database
        self.reviewer = reviewer.strip()

    def pending(self) -> list[ReviewItem]:
        rows = self.database.execute(
            "SELECT i.batch_id,s.source_id,s.root,s.relative_path,s.source_sha256,s.size_bytes,"
            "s.modified_ns,s.extraction_status,COALESCE(c.model_summary,''),"
            "COALESCE(c.proposed_topic,''),COALESCE(c.proposed_maturity,''),"
            "COALESCE(c.proposed_authority,''),COALESCE(c.proposal_reason,''),"
            "COALESCE(r.classification_confidence,0.0),"
            "COALESCE(r.review_route,'MANUAL_REVIEW_REQUIRED'),"
            "COALESCE(r.route_reasons,'candidate routing is unavailable') "
            "FROM review_batch_items i JOIN review_sources s ON s.source_id=i.source_id "
            "LEFT JOIN archive_review_candidates c ON c.source_id=s.source_id "
            "LEFT JOIN archive_review_routes r ON r.source_id=s.source_id "
            "WHERE NOT EXISTS (SELECT 1 FROM archive_review_decisions d "
            "WHERE d.source_id=s.source_id AND d.batch_id=i.batch_id) "
            "ORDER BY CASE COALESCE(r.review_route,'MANUAL_REVIEW_REQUIRED') "
            "WHEN 'RECOVERY_REQUIRED' THEN 0 WHEN 'MANUAL_REVIEW_REQUIRED' THEN 1 ELSE 2 END,"
            "i.batch_id,i.ordinal"
        ).fetchall()
        return [ReviewItem(row[0], row[1], row[2], row[3], row[4], row[6], row[7], row[8],
                           row[9], row[10], row[11], row[12], row[13], row[14], row[15])
                for row in rows]

    def accept(self, item: ReviewItem, decision: str, *, note: str,
               research_question: str = "") -> int:
        root = Path(item.root).resolve(strict=True)
        source = (root / item.source_path).resolve(strict=True)
        if not source.is_relative_to(root):
            raise ValueError("The source path escapes its admitted archive root")
        size = self.database.execute(
            "SELECT size_bytes FROM review_sources WHERE source_id=?", (item.source_id,)
        ).fetchone()
        if not size or _hash_stable_file(source, size[0]) != item.source_sha256:
            raise ValueError("The source hash changed after admission; review acceptance is blocked")
        row = {column: "" for column in REVIEW_COLUMNS}
        row.update({
            "batch_id": item.batch_id,
            "source_id": item.source_id,
            "source_path": item.source_path,
            "source_sha256": item.source_sha256,
            "modified_ns": str(item.modified_ns),
            "extraction_status": item.extraction_status,
            "model_summary": item.model_summary,
            "proposed_topic": item.proposed_topic,
            "proposed_maturity": item.proposed_maturity,
            "proposed_authority": item.proposed_authority,
            "proposal_reason": item.proposal_reason,
            "classification_confidence": str(item.classification_confidence),
            "review_route": item.review_route,
            "route_reasons": item.route_reasons,
            "relationship_candidates": "[]",
            "decision": decision,
            "topic": "Needs review",
            "maturity": "Needs review",
            "authority": "Non-authoritative candidate",
            "confidence": "UNKNOWN",
            "privacy_status": "Needs review",
            "canonical_status": "UNRESOLVED",
            "review_note": note.strip(),
            "research_question": research_question.strip(),
            "reviewer": self.reviewer,
        })
        return record_review_decision(self.database, row)


class ReviewWindow:
    def __init__(self, controller: ReviewController):
        import tkinter as tk
        from tkinter import messagebox

        self.tk = tk
        self.messagebox = messagebox
        self.controller = controller
        self.items = controller.pending()
        self.index = 0
        self.root = tk.Tk()
        self.root.title("Sovereign Workbench — Local Review")
        self.root.geometry("1000x760")
        self.root.minsize(820, 650)
        self.root.configure(bg="#f4f7fb")

        self.progress = tk.Label(self.root, font=("Segoe UI", 11, "bold"), bg="#f4f7fb")
        self.progress.pack(anchor="w", padx=24, pady=(20, 4))
        self.title = tk.Label(self.root, font=("Segoe UI", 16, "bold"), bg="#f4f7fb",
                              wraplength=930, justify="left")
        self.title.pack(anchor="w", padx=24, pady=(0, 8))
        self.route = tk.Label(self.root, font=("Segoe UI", 10, "bold"), fg="#8a3b12",
                              bg="#fff0df", padx=10, pady=6)
        self.route.pack(anchor="w", padx=24, pady=(0, 12))
        self.summary = self._text_box("Summary", 8)
        self.reason = self._text_box("Why it needs review", 4)
        self.question = self._text_box("Research question (editable)", 4, editable=True)
        self.note = self._text_box("Your review note (editable)", 3, editable=True)

        buttons = tk.Frame(self.root, bg="#f4f7fb")
        buttons.pack(fill="x", padx=24, pady=18)
        choices = [
            ("APPROVE", "#237a4b"), ("RESEARCH", "#275dad"),
            ("QUARANTINE", "#a65d00"), ("REJECT", "#a12a2a"),
        ]
        for label, colour in choices:
            tk.Button(buttons, text=label, command=lambda value=label: self.choose(value),
                      font=("Segoe UI", 12, "bold"), fg="white", bg=colour,
                      activebackground=colour, relief="flat", padx=18, pady=14
                      ).pack(side="left", expand=True, fill="x", padx=5)
        self.safety = tk.Label(
            self.root,
            text="This screen records review evidence only. It cannot move, delete, publish, stage, or train on files.",
            font=("Segoe UI", 10), fg="#39485a", bg="#f4f7fb", wraplength=930,
        )
        self.safety.pack(padx=24, pady=(0, 16))
        self.show_current()

    def _text_box(self, label: str, height: int, *, editable: bool = False):
        self.tk.Label(self.root, text=label, font=("Segoe UI", 10, "bold"),
                      bg="#f4f7fb").pack(anchor="w", padx=24)
        box = self.tk.Text(self.root, height=height, font=("Segoe UI", 10), wrap="word",
                           padx=10, pady=8, relief="solid", borderwidth=1)
        box.pack(fill="x", padx=24, pady=(3, 10))
        if not editable:
            box.configure(state="disabled", bg="#ffffff")
        return box

    @staticmethod
    def _set(box, value: str, *, editable: bool) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", value)
        if not editable:
            box.configure(state="disabled")

    def show_current(self) -> None:
        if self.index >= len(self.items):
            self.progress.configure(text="Review complete")
            self.title.configure(text="No review items remain in this database.")
            self.route.configure(text="All recorded decisions remain non-authoritative.")
            for box in (self.summary, self.reason, self.question, self.note):
                self._set(box, "", editable=box in (self.question, self.note))
            return
        item = self.items[self.index]
        self.progress.configure(text=f"File {self.index + 1} of {len(self.items)}")
        self.title.configure(text=item.source_path)
        self.route.configure(text=f"{item.review_route}  •  SHA-256 {item.source_sha256[:16]}…")
        self._set(self.summary, item.model_summary or "No summary is available.", editable=False)
        try:
            reasons = json.loads(item.route_reasons)
            reason = "\n".join(f"• {value}" for value in reasons) if isinstance(reasons, list) else str(reasons)
        except json.JSONDecodeError:
            reason = item.route_reasons
        self._set(self.reason, reason, editable=False)
        self._set(self.question, propose_research_question(item), editable=True)
        self._set(self.note, "Human reviewed the candidate summary and routing reason.", editable=True)

    def choose(self, label: str) -> None:
        if self.index >= len(self.items):
            return
        item = self.items[self.index]
        decision = "NEEDS_RESEARCH" if label == "RESEARCH" else label
        note = self.note.get("1.0", "end").strip()
        question = self.question.get("1.0", "end").strip() if decision == "NEEDS_RESEARCH" else ""
        try:
            inserted = self.controller.accept(item, decision, note=note,
                                              research_question=question)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.messagebox.showerror("Review blocked", str(exc))
            return
        if inserted != 1:
            self.messagebox.showerror("Review blocked", "This exact decision was already recorded.")
            return
        self.index += 1
        self.show_current()

    def run(self) -> None:
        self.root.mainloop()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="One-file-at-a-time local archive review")
    result.add_argument("--state-db", required=True, type=Path)
    result.add_argument("--reviewer", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    database = connect(args.state_db)
    try:
        ReviewWindow(ReviewController(database, args.reviewer)).run()
    finally:
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
