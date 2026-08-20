from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .adapters import AdapterError, request_sovereign_authorization, validate_with_bki
from .package import build_review_package, canonical_json, write_review_package
from .pipeline import analyze_workspace


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="skw", description="Local-first governed knowledge workbench")
    commands = root.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan", help="Analyze files without changing them")
    scan.add_argument("root", type=Path)
    scan.add_argument("--routes", type=Path)
    scan.add_argument("--local-model", action="store_true")
    scan.add_argument("--max-file-mb", type=int, default=50)
    scan.add_argument("--output", type=Path)
    scan.add_argument("--authorize-output", action="store_true", help="Request a Sovereign grant before writing output")

    validate = commands.add_parser("bki-validate", help="Invoke BKI's read-only validation boundary")
    validate.add_argument("--source", required=True, type=Path)
    validate.add_argument("--candidate", required=True, type=Path)
    validate.add_argument("--bki-root", required=True, type=Path)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "bki-validate":
            print(json.dumps(validate_with_bki(args.source, args.candidate, args.bki_root), ensure_ascii=False, sort_keys=True))
            return 0

        report = analyze_workspace(
            args.root,
            routes_path=args.routes,
            use_local_model=args.local_model,
            max_file_bytes=args.max_file_mb * 1024 * 1024,
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
    except (AdapterError, OSError, ValueError) as exc:
        print(f"workbench failed closed: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
