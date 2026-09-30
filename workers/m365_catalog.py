#!/usr/bin/env python3
"""Catalog helpers for the 365enum fzf interface."""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterator, TextIO


FIELDS = [
    "ID", "Site", "Library", "Path", "Name", "SizeMB", "SizeBytes", "Modified",
    "WebURL", "DriveID", "ItemID", "MimeType", "SharedScope",
]
INDEX = {name: index for index, name in enumerate(FIELDS)}
ALL_GROUP_KEY = "__all_files__"


def clean_cell(value: str) -> str:
    return value.replace("\t", " ").replace("\r", " ").replace("\n", " ")


def csv_rows(stream: TextIO) -> Iterator[list[str]]:
    reader = csv.reader(stream)
    for row in reader:
        if not row or row[:len(FIELDS)] == FIELDS or len(row) < len(FIELDS):
            continue
        yield [clean_cell(value) for value in row[:len(FIELDS)]]


def tsv_rows(stream: TextIO) -> Iterator[list[str]]:
    for line in stream:
        row = line.rstrip("\n").split("\t")
        if len(row) >= len(FIELDS):
            yield row[:len(FIELDS)]


def emit_tsv(row: list[str]) -> None:
    print("\t".join(clean_cell(value) for value in row))


def format_size_mb(size_mb: float) -> str:
    if size_mb >= 1024:
        return f"{size_mb / 1024:.3f} GB"
    return f"{size_mb:.3f} MB"


def command_catalog(args: argparse.Namespace) -> int:
    with args.csv.open(newline="", encoding="utf-8") as handle:
        for row in csv_rows(handle):
            emit_tsv(row)
    return 0


def command_id(args: argparse.Namespace) -> int:
    for row in tsv_rows(sys.stdin):
        if row[INDEX["ID"]] == args.id:
            emit_tsv(row)
            return 0
    print(f"No row found with ID {args.id}.", file=sys.stderr)
    return 1


def group_key(row: list[str], mode: str) -> str:
    if mode == "site":
        return row[INDEX["Site"]]
    if mode == "site-library":
        return f"{row[INDEX['Site']]}/{row[INDEX['Library']]}"
    if mode == "extension":
        return Path(row[INDEX["Name"]]).suffix.casefold() or "[no extension]"
    raise ValueError(f"unsupported mode {mode}")


def grouped(stream: TextIO, mode: str) -> dict[str, dict[str, float]]:
    groups: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "size": 0.0})
    for row in tsv_rows(stream):
        key = group_key(row, mode)
        groups[key]["count"] += 1
        try:
            groups[key]["size"] += float(row[INDEX["SizeMB"]])
        except ValueError:
            pass
    return groups


def command_groups(args: argparse.Namespace) -> int:
    groups = grouped(sys.stdin, args.mode)
    if args.include_all:
        count = sum(int(info["count"]) for info in groups.values())
        size = sum(info["size"] for info in groups.values())
        print(f"{ALL_GROUP_KEY}\t[All files] ({count} files, {format_size_mb(size)})")
    for key, info in sorted(groups.items(), key=lambda item: (-item[1]["count"], item[0])):
        print(f"{key}\t{key} ({int(info['count'])} files, {format_size_mb(info['size'])})")
    return 0


def command_filter(args: argparse.Namespace) -> int:
    for row in tsv_rows(sys.stdin):
        if group_key(row, args.mode) == args.key:
            emit_tsv(row)
    return 0


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    for row in rows:
        print("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))


def command_stats(args: argparse.Namespace) -> int:
    groups = grouped(sys.stdin, args.mode)
    rows = [[key, str(int(info["count"])), format_size_mb(info["size"])]
            for key, info in sorted(
                groups.items(),
                key=lambda item: (
                    -item[1]["count"] if args.sort == "desc" else item[1]["count"],
                    item[0],
                ),
            )]
    print_table([args.mode.title(), "Files", "Size"], rows)
    return 0


def command_sum(_args: argparse.Namespace) -> int:
    count = 0
    size = 0.0
    for row in tsv_rows(sys.stdin):
        count += 1
        try:
            size += float(row[INDEX["SizeMB"]])
        except ValueError:
            pass
    print(f"Selected: {count} files, {format_size_mb(size)}")
    return 0


def command_preview(args: argparse.Namespace) -> int:
    with args.row_file.open(encoding="utf-8") as handle:
        row = next(tsv_rows(handle), None)
    if row is None:
        return 1
    for field in FIELDS:
        if field not in {"DriveID", "ItemID"}:
            print(f"{field:12} {row[INDEX[field]]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="365enum catalog helpers")
    sub = parser.add_subparsers(dest="command", required=True)
    catalog = sub.add_parser("catalog")
    catalog.add_argument("csv", type=Path)
    catalog.set_defaults(func=command_catalog)
    by_id = sub.add_parser("id")
    by_id.add_argument("--id", required=True)
    by_id.set_defaults(func=command_id)
    groups = sub.add_parser("groups")
    groups.add_argument("--mode", choices=["site", "site-library", "extension"], required=True)
    groups.add_argument("--include-all", action="store_true")
    groups.set_defaults(func=command_groups)
    filter_parser = sub.add_parser("filter")
    filter_parser.add_argument("--mode", choices=["site", "site-library", "extension"], required=True)
    filter_parser.add_argument("--key", required=True)
    filter_parser.set_defaults(func=command_filter)
    stats = sub.add_parser("stats")
    stats.add_argument("--mode", choices=["site", "site-library", "extension"], required=True)
    stats.add_argument("--sort", choices=["asc", "desc"], default="asc")
    stats.set_defaults(func=command_stats)
    sum_parser = sub.add_parser("sum")
    sum_parser.set_defaults(func=command_sum)
    preview = sub.add_parser("preview")
    preview.add_argument("row_file", type=Path)
    preview.set_defaults(func=command_preview)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
