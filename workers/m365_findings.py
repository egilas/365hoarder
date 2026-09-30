#!/usr/bin/env python3
"""Create metadata findings from an existing 365hoarder scan directory."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from m365_output import error, info, success


DEFAULT_FILENAME_TERMS = {
    "backup", "breakglass", "confidential", "credential", "credentials", "finance",
    "hr", "password", "passwords", "payroll", "private key", "recovery code", "secret",
    "secrets", "sensitive", "token", "vpn",
}
DEFAULT_SENSITIVE_EXTENSIONS = {
    ".bak", ".cer", ".crt", ".env", ".key", ".kdbx", ".ovpn", ".p12", ".pfx",
    ".pem", ".ppk", ".rdp", ".sql", ".sqlite",
}
CONTEXT_FIELDS = (
    "site_name", "site_url", "drive_id", "drive_name", "item_id", "name", "web_url",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_extension(value: str) -> str:
    extension = value.casefold().strip()
    if not extension:
        raise ValueError("extension cannot be empty")
    return extension if extension.startswith(".") else f".{extension}"


def string_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be an array of strings")
    return value


def load_rules(path: Path | None, include_defaults: bool = True) -> tuple[set[str], set[str]]:
    terms = set(DEFAULT_FILENAME_TERMS) if include_defaults else set()
    extensions = set(DEFAULT_SENSITIVE_EXTENSIONS) if include_defaults else set()
    if path is None:
        return terms, extensions
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("rules file must contain a JSON object")
    unknown = sorted(set(value) - {"filename_terms", "sensitive_extensions"})
    if unknown:
        raise ValueError(f"unsupported rules key(s): {', '.join(unknown)}")
    terms.update(
        term.casefold().strip()
        for term in string_list(value.get("filename_terms"), "filename_terms")
        if term.strip()
    )
    extensions.update(
        normalize_extension(extension)
        for extension in string_list(
            value.get("sensitive_extensions"), "sensitive_extensions"
        )
    )
    return terms, extensions


def inventory_rows(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on {path}:{line_number}: {exc.msg}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"expected a JSON object on {path}:{line_number}")
            yield value


def item_findings(
    item: dict[str, Any], terms: set[str], extensions: set[str]
) -> Iterator[dict[str, Any]]:
    name = str(item.get("name") or "")
    folded_name = name.casefold()
    context = {field: item.get(field) for field in CONTEXT_FIELDS}
    observed_at = utc_now()
    for term in sorted(terms):
        if term and term in folded_name:
            yield {
                "observed_at": observed_at,
                **context,
                "rule_id": "interesting-filename",
                "title": "Interesting filename term",
                "severity": "low",
                "matched_term": term,
            }
    extension = Path(name).suffix.casefold()
    if extension in extensions:
        yield {
            "observed_at": observed_at,
            **context,
            "rule_id": "sensitive-extension",
            "title": "Potentially sensitive file type",
            "severity": "medium",
            "extension": extension,
        }


def write_findings(
    inventory: Path, output: Path, terms: set[str], extensions: set[str]
) -> tuple[int, int, Counter[str]]:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", dir=output.parent, text=True
    )
    temporary = Path(temporary_name)
    files = 0
    findings = 0
    counts: Counter[str] = Counter()
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for item in inventory_rows(inventory):
                files += 1
                for finding in item_findings(item, terms, extensions):
                    handle.write(json.dumps(finding, ensure_ascii=False, sort_keys=True) + "\n")
                    findings += 1
                    counts[str(finding["rule_id"])] += 1
        os.replace(temporary, output)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    return files, findings, counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create findings.jsonl from metadata in a 365hoarder scan directory.",
        epilog=(
            "This is an offline metadata pass. It examines filenames and extensions in "
            "inventory.jsonl; it does not authenticate, download, or inspect file bodies."
        ),
    )
    parser.add_argument("scan_dir", type=Path, metavar="SCAN_DIR",
                        help="scan-* directory containing inventory.jsonl")
    parser.add_argument("--rules", type=Path,
                        help="JSON file with additive filename_terms and sensitive_extensions")
    parser.add_argument("--term", action="append", default=[], metavar="TEXT",
                        help="add a case-insensitive filename term; repeat as needed")
    parser.add_argument("--extension", action="append", default=[], metavar="EXT",
                        help="add a sensitive extension; repeat as needed")
    parser.add_argument("--no-defaults", action="store_true",
                        help="disable built-in terms and extensions")
    parser.add_argument("--output", type=Path, metavar="FILE",
                        help="output path (default: SCAN_DIR/findings.jsonl)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    inventory = args.scan_dir / "inventory.jsonl"
    if not args.scan_dir.is_dir() or not inventory.is_file():
        error(f"{args.scan_dir} does not contain inventory.jsonl")
        return 2
    output = args.output or args.scan_dir / "findings.jsonl"
    try:
        if output.resolve() == inventory.resolve():
            raise ValueError("output must not overwrite inventory.jsonl")
        terms, extensions = load_rules(args.rules, include_defaults=not args.no_defaults)
        terms.update(term.casefold().strip() for term in args.term if term.strip())
        extensions.update(normalize_extension(value) for value in args.extension)
        if not terms and not extensions:
            raise ValueError("no finding rules are enabled")
        info(
            f"Findings: scanning {inventory} with {len(terms)} filename terms and "
            f"{len(extensions)} extensions."
        )
        files, finding_count, counts = write_findings(
            inventory, output, terms, extensions
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        error(f"could not create findings: {exc}")
        return 2
    detail = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
    suffix = f" ({detail})" if detail else ""
    success(f"Wrote {finding_count} findings from {files} files to {output}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
