from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .adapters import AdapterError, request_sovereign_authorization, validate_with_bki
from .package import build_review_package, canonical_json, write_review_package
from .pipeline import analyze_workspace
from .plugins import PluginRuntimeError, list_plugins, run_plugin
from .jobs import connect, enqueue, run_pending, status_counts
from .roles import RolePolicyError, evaluate_tool_eligibility, load_role_policy


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="skw", description="Local-first governed knowledge workbench")
    commands = root.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan", help="Analyze files without changing them")
    scan.add_argument("root", type=Path)
    scan.add_argument("--routes", type=Path)
    scan.add_argument("--local-model", action="store_true")
    scan.add_argument("--max-file-mb", type=int, default=50)
    scan.add_argument("--max-files", type=int)
    scan.add_argument("--include", nargs="+", metavar="SUFFIX", help="Only scan suffixes such as .md .txt .pdf")
    scan.add_argument("--output", type=Path)
    scan.add_argument("--authorize-output", action="store_true", help="Request a Sovereign grant before writing output")

    validate = commands.add_parser("bki-validate", help="Invoke BKI's read-only validation boundary")
    validate.add_argument("--source", required=True, type=Path)
    validate.add_argument("--candidate", required=True, type=Path)
    validate.add_argument("--bki-root", required=True, type=Path)

    commands.add_parser("plugin-list", help="List installed governed plugins")
    plugin = commands.add_parser("plugin-run", help="Run one hash-bound candidate-only plugin")
    plugin.add_argument("plugin_id")
    plugin.add_argument("path", type=Path)
    plugin.add_argument("--max-file-mb", type=int, default=100)

    batch = commands.add_parser("plugin-batch", help="Queue and run a bounded resumable plugin batch")
    batch.add_argument("plugin_id")
    batch.add_argument("root", type=Path)
    batch.add_argument("--state-db", required=True, type=Path)
    batch.add_argument("--include", nargs="+", required=True)
    batch.add_argument("--limit", type=int, default=25)
    batch.add_argument("--max-file-mb", type=int, default=100)
    batch.add_argument("--role", required=True)
    batch.add_argument("--roles", required=True, type=Path)

    jobs = commands.add_parser("jobs-status", help="Show durable plugin job counts")
    jobs.add_argument("--state-db", required=True, type=Path)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "plugin-list":
            print(json.dumps(list_plugins(), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "jobs-status":
            with connect(args.state_db) as database:
                print(json.dumps(status_counts(database), sort_keys=True))
            return 0
        if args.command == "plugin-batch":
            from sovereign_plugins.contracts import hash_file
            eligibility = evaluate_tool_eligibility(load_role_policy(args.roles), args.role, args.plugin_id)
            if not eligibility["eligible"]:
                raise RolePolicyError(eligibility["reason"])
            suffixes = {value.casefold() if value.startswith(".") else f".{value.casefold()}" for value in args.include}
            maximum = args.max_file_mb * 1024 * 1024
            with connect(args.state_db) as database:
                admitted = 0
                for path in sorted(args.root.resolve(strict=True).rglob("*")):
                    if admitted >= args.limit or path.is_symlink() or not path.is_file() or path.suffix.casefold() not in suffixes:
                        continue
                    enqueue(database, args.plugin_id, path, hash_file(path, maximum))
                    admitted += 1
                outcome = run_pending(database, limit=args.limit)
                print(json.dumps({"admitted": admitted, **outcome, "status": status_counts(database)}, sort_keys=True))
            return 0
        if args.command == "plugin-run":
            print(json.dumps(
                run_plugin(args.plugin_id, args.path, max_bytes=args.max_file_mb * 1024 * 1024),
                ensure_ascii=False,
                sort_keys=True,
            ))
            return 0
        if args.command == "bki-validate":
            print(json.dumps(validate_with_bki(args.source, args.candidate, args.bki_root), ensure_ascii=False, sort_keys=True))
            return 0

        report = analyze_workspace(
            args.root,
            routes_path=args.routes,
            use_local_model=args.local_model,
            max_file_bytes=args.max_file_mb * 1024 * 1024,
            include_suffixes={value.casefold() if value.startswith(".") else f".{value.casefold()}" for value in args.include} if args.include else None,
            max_files=args.max_files,
        )
        package = build_review_package(report)
        if args.output:
            if not args.authorize_output:
                raise AdapterError("Writing a review package requires --authorize-output and a configured Sovereign authorizer")
            request_sovereign_authorization({
                "operation": "write_review_package",
                "target": str(args.output.resolve()),
                "report_sha256": package["report_sha256"],
            })
            write_review_package(package, args.output)
        else:
            sys.stdout.buffer.write(canonical_json(package))
        return 0
    except (AdapterError, PluginRuntimeError, RolePolicyError, OSError, ValueError) as exc:
        print(f"workbench failed closed: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
