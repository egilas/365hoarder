#!/usr/bin/env python3
"""Download selected 365enum catalog rows using delegated Graph access."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from m365_auth import (
    access_token_from_environment,
    refresh_credentials_available,
    resolve_token_source,
)
from m365_catalog import INDEX, tsv_rows
from m365_output import accent, error, success, warning
from sharepoint_enum import GraphClient, extract_text


def safe_component(value: str, limit: int = 100) -> str:
    value = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", value).strip(" .")
    return (value or "_")[:limit]


def output_path(output: Path, row: list[str]) -> Path:
    extension = Path(row[INDEX["Name"]]).suffix
    stem = safe_component(Path(row[INDEX["Name"]]).stem, 80)
    site = safe_component(row[INDEX["Site"]], 50)
    library = safe_component(row[INDEX["Library"]], 50)
    identity = safe_component(row[INDEX["ID"]], 20)
    return output / f"{identity}-{site}__{library}__{stem}{extension}"


def view_file(path: Path) -> None:
    if not shutil.which("fzf"):
        error("fzf is required for --view")
        return
    try:
        text = extract_text(path.name, path.read_bytes())
    except Exception as exc:
        warning(f"No text preview available for {path}: {exc}")
        return
    subprocess.run(
        ["fzf", "--no-sort", "--prompt", f"view {path.name} > ", "--header", str(path)],
        input=text[:5_000_000],
        text=True,
        check=False,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download selected 365enum rows")
    parser.add_argument("-c", "--config", type=Path, default=Path("./creds.ini"))
    parser.add_argument("-s", "--section", default="DEFAULT")
    parser.add_argument("--output", type=Path, default=Path("dl"))
    parser.add_argument("--max-download-mb", type=float, default=500.0)
    parser.add_argument("--view", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.max_download_mb <= 0:
        raise SystemExit("--max-download-mb must be greater than zero")
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(args.output, 0o700)
    except OSError:
        pass

    if not access_token_from_environment() and not refresh_credentials_available(args):
        raise SystemExit("M365AT or creds.ini is required")
    graph = GraphClient(resolve_token_source(args))
    failed = False
    for row in tsv_rows(sys.stdin):
        drive_id = row[INDEX["DriveID"]]
        item_id = row[INDEX["ItemID"]]
        destination = output_path(args.output, row)
        try:
            limit = int(args.max_download_mb * 1024 * 1024)
            data = graph.download(drive_id, item_id, limit)
            destination.write_bytes(data)
            try:
                os.chmod(destination, 0o600)
            except OSError:
                pass
            if not args.quiet:
                success(f"Downloaded: {destination}")
                accent(f"SharePoint URL: {row[INDEX['WebURL']]}")
            if args.view:
                view_file(destination)
        except Exception as exc:
            error(f"Download failed for ID {row[INDEX['ID']]}: {exc}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
