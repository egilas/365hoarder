#!/usr/bin/env python3
"""Read-only Microsoft 365 file enumerator for authorized assessments."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import re
import sys
import time
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote

import requests
from m365_auth import (
    access_token_from_environment,
    refresh_credentials_available,
    resolve_token_source,
)
from m365_output import error as console_error
from m365_output import info, success, warning


GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
TRANSIENT_STATUS = {429, 500, 502, 503, 504}

TEXT_EXTENSIONS = {
    ".asc", ".bat", ".cfg", ".conf", ".config", ".csv", ".env", ".gitconfig",
    ".htm", ".html", ".ini", ".java", ".js", ".json", ".log", ".md", ".pem",
    ".properties", ".ps1", ".py", ".rb", ".sh", ".sql", ".toml", ".ts", ".txt",
    ".xml", ".yaml", ".yml",
}
OFFICE_EXTENSIONS = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}
PDF_EXTENSIONS = {".pdf"}
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_error(value: Any, limit: int = 500) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text[:limit]


class JsonlWriter:
    def __init__(self, path: Path):
        self.path = path
        self.handle = path.open("a", encoding="utf-8")

    def write(self, value: dict[str, Any]) -> None:
        self.handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        self.handle.flush()

    def close(self) -> None:
        self.handle.close()


INVENTORY_CSV_FIELDS = [
    "ID", "Site", "Library", "Path", "Name", "SizeMB", "SizeBytes", "Modified",
    "WebURL", "DriveID", "ItemID", "MimeType", "SharedScope",
]


class InventoryCsvWriter:
    def __init__(self, path: Path):
        self.handle = path.open("w", encoding="utf-8", newline="")
        self.writer = csv.DictWriter(self.handle, fieldnames=INVENTORY_CSV_FIELDS)
        self.writer.writeheader()

    def write(self, item_id: int, value: dict[str, Any]) -> None:
        size = value.get("size")
        parent_path = str(value.get("parent_path") or "")
        relative_parent = parent_path.split("root:", 1)[-1].strip("/")
        name = str(value.get("name") or "")
        relative_path = "/".join(part for part in (relative_parent, name) if part)
        self.writer.writerow({
            "ID": item_id,
            "Site": value.get("site_name") or "OneDrive/shared",
            "Library": value.get("drive_name") or "",
            "Path": relative_path,
            "Name": name,
            "SizeMB": f"{size / 1024 / 1024:.3f}" if isinstance(size, int) else "",
            "SizeBytes": size if isinstance(size, int) else "",
            "Modified": value.get("modified_at") or "",
            "WebURL": value.get("web_url") or "",
            "DriveID": value.get("drive_id") or "",
            "ItemID": value.get("item_id") or "",
            "MimeType": value.get("mime_type") or "",
            "SharedScope": value.get("shared_scope") or "",
        })
        self.handle.flush()

    def close(self) -> None:
        self.handle.close()


class GraphError(RuntimeError):
    def __init__(self, status: int, method: str, url: str, detail: str):
        super().__init__(f"Graph {method} {url} returned HTTP {status}: {detail}")
        self.status = status
        self.method = method
        self.url = url
        self.detail = detail


class GraphClient:
    def __init__(self, access_token: str | Callable[[], str], timeout: int = 60,
                 max_retries: int = 8):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "365enum/1.0",
        })
        self._token_source = access_token
        self.timeout = timeout
        self.max_retries = max_retries

    @staticmethod
    def absolute_url(path_or_url: str) -> str:
        if path_or_url.startswith("https://"):
            if not path_or_url.startswith("https://graph.microsoft.com/"):
                raise ValueError("Refusing non-Microsoft Graph pagination URL")
            return path_or_url
        return GRAPH_ROOT + (path_or_url if path_or_url.startswith("/") else "/" + path_or_url)

    def request(self, method: str, path_or_url: str, **kwargs: Any) -> requests.Response:
        url = self.absolute_url(path_or_url)
        auth_refreshed = False
        for attempt in range(self.max_retries + 1):
            try:
                token = self._token_source() if callable(self._token_source) else self._token_source
                self.session.headers["Authorization"] = f"Bearer {token}"
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException:
                if attempt >= self.max_retries:
                    raise
                time.sleep(min(60.0, (2 ** attempt) + random.random()))
                continue
            if (response.status_code == 401 and callable(self._token_source)
                    and not auth_refreshed and attempt < self.max_retries):
                invalidate = getattr(self._token_source, "invalidate", None)
                if callable(invalidate):
                    invalidate()
                auth_refreshed = True
                continue
            if response.status_code not in TRANSIENT_STATUS:
                return response
            if attempt >= self.max_retries:
                return response
            retry_after = response.headers.get("Retry-After", "")
            try:
                delay = float(retry_after)
            except ValueError:
                delay = min(60.0, (2 ** attempt) + random.random())
            time.sleep(max(0.0, min(delay, 120.0)))
        raise AssertionError("unreachable")

    def get_json(self, path_or_url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        response = self.request("GET", path_or_url, params=params)
        if not response.ok:
            try:
                detail = response.json().get("error", {}).get("message", response.text)
            except (ValueError, AttributeError):
                detail = response.text
            raise GraphError(response.status_code, "GET", response.url, compact_error(detail))
        return response.json()

    def pages(self, path: str, params: dict[str, str] | None = None) -> Iterator[dict[str, Any]]:
        next_url: str | None = path
        next_params = params
        while next_url:
            page = self.get_json(next_url, params=next_params)
            yield page
            next_url = page.get("@odata.nextLink")
            next_params = None

    def values(self, path: str, params: dict[str, str] | None = None) -> Iterator[dict[str, Any]]:
        for page in self.pages(path, params=params):
            for value in page.get("value", []):
                if isinstance(value, dict):
                    yield value

    def download(self, drive_id: str, item_id: str, size_limit: int) -> bytes:
        path = f"/drives/{quote(drive_id, safe='')}/items/{quote(item_id, safe='')}/content"
        response = self.request("GET", path, stream=True)
        if not response.ok:
            raise GraphError(response.status_code, "GET", path, compact_error(response.text))
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > size_limit:
            response.close()
            raise ValueError(f"download response exceeds {size_limit} bytes")
        chunks: list[bytes] = []
        received = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            received += len(chunk)
            if received > size_limit:
                response.close()
                raise ValueError(f"download exceeded {size_limit} bytes")
            chunks.append(chunk)
        return b"".join(chunks)


def decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace")
    return data.decode("utf-8", errors="replace")


def extract_zip_xml(data: bytes) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            name = info.filename.casefold()
            if not name.endswith(".xml"):
                continue
            if not (name.startswith(("word/", "xl/", "ppt/")) or name in {"content.xml", "styles.xml"}):
                continue
            if info.file_size > 20 * 1024 * 1024:
                continue
            raw = archive.read(info)
            text = re.sub(r"<[^>]+>", " ", raw.decode("utf-8", errors="replace"))
            parts.append(re.sub(r"\s+", " ", text))
    return "\n".join(parts)


def extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                raise ValueError("encrypted PDF")
        except Exception as exc:
            raise ValueError("encrypted PDF") from exc
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def extract_text(name: str, data: bytes) -> str:
    extension = Path(name).suffix.casefold()
    if extension in TEXT_EXTENSIONS:
        return decode_text(data)
    if extension in OFFICE_EXTENSIONS:
        return extract_zip_xml(data)
    if extension in PDF_EXTENSIONS:
        return extract_pdf(data)
    raise ValueError(f"unsupported extension {extension or '<none>'}")


def public_item(item: dict[str, Any], drive: dict[str, Any], site: dict[str, Any] | None) -> dict[str, Any]:
    parent = item.get("parentReference") or {}
    file_facet = item.get("file") or {}
    return {
        "observed_at": utc_now(),
        "site_id": site.get("id") if site else None,
        "site_name": (site.get("displayName") or site.get("name")) if site else None,
        "site_url": site.get("webUrl") if site else None,
        "drive_id": drive.get("id"),
        "drive_name": drive.get("name"),
        "drive_type": drive.get("driveType"),
        "item_id": item.get("id"),
        "name": item.get("name"),
        "web_url": item.get("webUrl"),
        "parent_path": parent.get("path"),
        "size": item.get("size"),
        "created_at": item.get("createdDateTime"),
        "modified_at": item.get("lastModifiedDateTime"),
        "created_by": ((item.get("createdBy") or {}).get("user") or {}).get("displayName"),
        "modified_by": ((item.get("lastModifiedBy") or {}).get("user") or {}).get("displayName"),
        "mime_type": file_facet.get("mimeType"),
        "sha1_hash": ((file_facet.get("hashes") or {}).get("sha1Hash")),
        "shared_scope": (item.get("shared") or {}).get("scope"),
    }


@dataclass
class ScanState:
    output: Path
    status_every: int = 1000
    counts: Counter[str] = field(default_factory=Counter)
    seen_drives: set[str] = field(default_factory=set)
    seen_items: set[tuple[str, str]] = field(default_factory=set)
    writers: dict[str, JsonlWriter] = field(init=False)
    inventory_csv: InventoryCsvWriter = field(init=False)
    status_started: float = field(default_factory=time.monotonic, init=False)
    last_status_marker: int = field(default=0, init=False)
    search_total_hint: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.output.mkdir(parents=True, exist_ok=False)
        try:
            os.chmod(self.output, 0o700)
        except OSError:
            pass
        self.writers = {name: JsonlWriter(self.output / f"{name}.jsonl")
                        for name in ("sites", "drives", "inventory", "errors")}
        self.inventory_csv = InventoryCsvWriter(self.output / "inventory.csv")

    def write(self, stream: str, value: dict[str, Any]) -> None:
        self.writers[stream].write(value)
        self.counts[stream] += 1
        if stream == "inventory":
            self.inventory_csv.write(self.counts[stream], value)
            self.report_progress()

    def report_progress(self, scanned: int | None = None, total: int | None = None,
                        force: bool = False) -> None:
        if self.status_every <= 0:
            return
        if total and total > 0:
            self.search_total_hint = total
        marker = max(self.counts["inventory"], scanned or 0)
        if not force:
            if self.status_every <= 0 or marker <= 0:
                return
            if marker - self.last_status_marker < self.status_every:
                return
        self.last_status_marker = marker
        elapsed = max(time.monotonic() - self.status_started, 0.001)
        rate = self.counts["inventory"] / elapsed
        total_text = (f" | search-total={self.search_total_hint}"
                      if self.search_total_hint else "")
        info(
            f"Status: files={self.counts['inventory']}{total_text} | "
            f"libraries={self.counts['drives']} | errors={self.counts['errors']} | "
            f"elapsed={elapsed:.0f}s | "
            f"rate={rate:.1f} files/s"
        )

    def error(self, stage: str, exc: Exception, **context: Any) -> None:
        self.write("errors", {
            "observed_at": utc_now(),
            "stage": stage,
            "error_type": type(exc).__name__,
            "error": compact_error(exc),
            **context,
        })

    def close(self) -> None:
        for writer in self.writers.values():
            writer.close()
        self.inventory_csv.close()


def discover_sites(graph: GraphClient, state: ScanState) -> list[dict[str, Any]]:
    sites: dict[str, dict[str, Any]] = {}
    try:
        root = graph.get_json("/sites/root", params={"$select": "id,name,displayName,webUrl"})
        if root.get("id"):
            sites[root["id"]] = root
    except Exception as exc:
        state.error("discover-root-site", exc)
    try:
        params = {"search": "*", "$select": "id,name,displayName,webUrl,createdDateTime"}
        for site in graph.values("/sites", params=params):
            if site.get("id"):
                sites[site["id"]] = site
    except Exception as exc:
        state.error("discover-sites", exc)
    ordered = sorted(sites.values(), key=lambda site: (site.get("webUrl") or "").casefold())
    for site in ordered:
        state.write("sites", {"observed_at": utc_now(), **site})
    return ordered


def add_drive(state: ScanState, drive: dict[str, Any], source: str,
              site: dict[str, Any] | None) -> bool:
    drive_id = drive.get("id")
    if not drive_id or drive_id in state.seen_drives:
        return False
    state.seen_drives.add(drive_id)
    state.write("drives", {
        "observed_at": utc_now(),
        "source": source,
        "site_id": site.get("id") if site else None,
        "site_name": (site.get("displayName") or site.get("name")) if site else None,
        "site_url": site.get("webUrl") if site else None,
        "id": drive_id,
        "name": drive.get("name"),
        "drive_type": drive.get("driveType"),
        "web_url": drive.get("webUrl"),
        "owner": (((drive.get("owner") or {}).get("user") or {}).get("displayName")),
    })
    return True


def inventory_drive(graph: GraphClient, state: ScanState, drive: dict[str, Any],
                    site: dict[str, Any] | None) -> None:
    drive_id = str(drive["id"])
    select = ("id,name,webUrl,size,createdDateTime,lastModifiedDateTime,createdBy,"
              "lastModifiedBy,parentReference,file,folder,package,remoteItem,shared,deleted")
    path = f"/drives/{quote(drive_id, safe='')}/root/delta"
    try:
        for page in graph.pages(path, params={"$select": select}):
            for item in page.get("value", []):
                if not isinstance(item, dict) or item.get("deleted") or not item.get("file"):
                    continue
                inspect_item(graph, state, drive, site, item)
    except Exception as exc:
        state.error("inventory-drive", exc, drive_id=drive_id, drive_name=drive.get("name"))


def inspect_item(graph: GraphClient, state: ScanState, drive: dict[str, Any],
                 site: dict[str, Any] | None, item: dict[str, Any]) -> None:
    drive_id, item_id = str(drive["id"]), str(item.get("id", ""))
    if not item_id or (drive_id, item_id) in state.seen_items:
        return
    state.seen_items.add((drive_id, item_id))
    base = public_item(item, drive, site)
    state.write("inventory", base)


def inspect_shared_items(graph: GraphClient, state: ScanState) -> None:
    try:
        for item in graph.values("/me/drive/sharedWithMe", params={"allowexternal": "true"}):
            remote = item.get("remoteItem") or item
            parent = remote.get("parentReference") or {}
            drive_id = parent.get("driveId")
            item_id = remote.get("id")
            if not drive_id or not item_id or not remote.get("file"):
                continue
            drive = {"id": drive_id, "name": "Shared with me", "driveType": "shared"}
            inspect_item(graph, state, drive, None, remote)
    except Exception as exc:
        state.error("shared-with-me", exc)


def run_scan(args: argparse.Namespace) -> int:
    output = args.output or Path(f"scan-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    state = ScanState(output=output, status_every=args.status_every)
    started_at = utc_now()
    exit_code = 0
    try:
        token_source = resolve_token_source(args)
        graph = GraphClient(
            token_source, timeout=args.timeout, max_retries=args.max_retries
        )
        profile = graph.get_json("/me", params={"$select": "id,displayName,userPrincipalName"})
        success(
            f"Signed in as {profile.get('userPrincipalName') or profile.get('displayName')}"
        )

        sites = discover_sites(graph, state)
        success(f"Discovered {len(sites)} SharePoint site(s).")

        drives_to_scan: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        try:
            drive = graph.get_json("/me/drive", params={"$select": "id,name,driveType,webUrl,owner"})
            if add_drive(state, drive, "onedrive", None):
                drives_to_scan.append((drive, None))
        except Exception as exc:
            state.error("discover-onedrive", exc)

        for index, site in enumerate(sites, start=1):
            info(f"Discovering libraries [{index}/{len(sites)}]: {site.get('webUrl')}")
            try:
                site_id = quote(str(site["id"]), safe="")
                params = {"$select": "id,name,driveType,webUrl,owner"}
                for drive in graph.values(f"/sites/{site_id}/drives", params=params):
                    if add_drive(state, drive, "sharepoint-site", site):
                        drives_to_scan.append((drive, site))
            except Exception as exc:
                state.error("discover-site-drives", exc, site_id=site.get("id"),
                            site_url=site.get("webUrl"))

        success(f"Discovered {len(drives_to_scan)} unique drive(s).")
        for index, (drive, site) in enumerate(drives_to_scan, start=1):
            label = drive.get("webUrl") or drive.get("name") or drive.get("id")
            info(f"Inventorying drive [{index}/{len(drives_to_scan)}]: {label}")
            inventory_drive(graph, state, drive, site)

        if args.shared_items:
            info("Checking items shared directly with the signed-in user.")
            inspect_shared_items(graph, state)
    except KeyboardInterrupt:
        warning("Interrupted; partial output has been preserved.")
        exit_code = 130
    except Exception as exc:
        state.error("fatal", exc)
        console_error(f"fatal: {compact_error(exc)}")
        exit_code = 1
    finally:
        state.report_progress(force=True)
        summary = {
            "started_at": started_at,
            "finished_at": utc_now(),
            "include_shared_items": args.shared_items,
            "authentication": (
                "supplied-graph-token" if access_token_from_environment()
                else "refresh-token"
            ),
            "counts": dict(sorted(state.counts.items())),
            "exit_code": exit_code,
        }
        (state.output / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        state.close()
    success(f"Results written to {state.output.resolve()}")
    return exit_code


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def jsonl_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as handle:
        return sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(1024 * 1024), b""))


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024.0 or unit == "PiB":
            return f"{value:.2f} {unit}"
        value /= 1024.0
    raise AssertionError("unreachable")


def resolve_stats_directory(value: str) -> Path:
    if value != "__latest__":
        directory = Path(value)
    else:
        candidates = [
            path for path in Path(".").glob("scan-*")
            if path.is_dir() and (path / "inventory.csv").is_file()
        ]
        if not candidates:
            raise FileNotFoundError("no scan-* directory containing inventory.csv was found")
        directory = max(candidates, key=lambda path: path.stat().st_mtime)
    if not directory.is_dir() or not (directory / "inventory.csv").is_file():
        raise FileNotFoundError(f"{directory} does not contain inventory.csv")
    return directory


def print_ranked_stats(title: str, values: dict[str, list[int]], limit: int = 10) -> None:
    print(f"\n{title}")
    print(f"{'Files':>8}  {'Size':>12}  Name")
    for name, (count, size) in sorted(
            values.items(), key=lambda item: (-item[1][0], -item[1][1], item[0]))[:limit]:
        label = name or "[unknown]"
        print(f"{count:>8}  {human_size(size):>12}  {label}")


def print_scan_stats(value: str) -> int:
    directory = resolve_stats_directory(value)
    summary_path = directory / "summary.json"
    summary: dict[str, Any] = {}
    if summary_path.exists():
        loaded = json.loads(summary_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            summary = loaded

    site_stats: dict[str, list[int]] = {}
    library_stats: dict[str, list[int]] = {}
    extension_stats: dict[str, list[int]] = {}
    file_count = 0
    total_size = 0
    with (directory / "inventory.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            file_count += 1
            try:
                size = int(row.get("SizeBytes") or 0)
            except ValueError:
                size = 0
            total_size += size
            site = row.get("Site") or "[unknown]"
            library = f"{site}/{row.get('Library') or '[unknown]'}"
            extension = Path(row.get("Name") or "").suffix.casefold() or "[no extension]"
            for target, key in (
                    (site_stats, site), (library_stats, library),
                    (extension_stats, extension)):
                stats = target.setdefault(key, [0, 0])
                stats[0] += 1
                stats[1] += size

    started = str(summary.get("started_at") or "")
    finished = str(summary.get("finished_at") or "")
    duration = ""
    if started and finished:
        try:
            seconds = (datetime.fromisoformat(finished) -
                       datetime.fromisoformat(started)).total_seconds()
            duration = f"{seconds:.1f}s"
        except ValueError:
            pass
    exit_code = summary.get("exit_code")
    status = "complete" if exit_code == 0 else ("partial/running" if exit_code is None else "failed")
    print(f"Scan:           {directory.resolve()}")
    print(f"Status:         {status}")
    if summary.get("authentication"):
        print(f"Authentication: {summary['authentication']}")
    if started:
        print(f"Started:        {started}")
    if finished:
        print(f"Finished:       {finished}")
    if duration:
        print(f"Duration:       {duration}")
    print(f"Sites:          {jsonl_count(directory / 'sites.jsonl')}")
    print(f"Libraries:      {jsonl_count(directory / 'drives.jsonl')}")
    print(f"Files:          {file_count}")
    print(f"Total size:     {human_size(total_size)}")
    print(f"Findings:       {jsonl_count(directory / 'findings.jsonl')}")
    print(f"Errors:         {jsonl_count(directory / 'errors.jsonl')}")
    print_ranked_stats("Top sites", site_stats)
    print_ranked_stats("Top libraries", library_stats)
    print_ranked_stats("Top file types", extension_stats)
    return 0


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inventory files available to a Microsoft 365 user.")
    parser.add_argument(
        "--stats", nargs="?", const="__latest__", metavar="SCAN_DIR",
        help="Print offline statistics for SCAN_DIR (default: newest scan-*) and exit")
    parser.add_argument("-cfile", "--config", type=Path, default=Path("./creds.ini"),
                        help="Refresh-token credential file (default: ./creds.ini)")
    parser.add_argument("-s", "--section", default="DEFAULT",
                        help="Credential file section (default: DEFAULT)")
    parser.add_argument("--output", type=Path, help="New output directory (default: timestamped)")
    parser.add_argument("--timeout", type=positive_int, default=60,
                        help="HTTP timeout in seconds (default: 60)")
    parser.add_argument("--max-retries", type=positive_int, default=8,
                        help="Transient Graph request retries (default: 8)")
    parser.add_argument("--status-every", type=non_negative_int, default=1000,
                        help="Print progress every N files/results; 0 disables (default: 1000)")
    parser.add_argument("--no-shared-items", dest="shared_items", action="store_false",
                        help="Skip the deprecated sharedWithMe supplementary pass")
    parser.set_defaults(shared_items=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.stats is not None:
        try:
            return print_scan_stats(args.stats)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            console_error(f"could not read scan statistics: {compact_error(exc)}")
            return 2
    if not access_token_from_environment() and not refresh_credentials_available(args):
        parser.error("set M365AT or provide creds.ini")
    return run_scan(args)


if __name__ == "__main__":
    raise SystemExit(main())
