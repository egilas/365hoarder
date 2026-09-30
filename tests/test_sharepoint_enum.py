import csv
import io
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workers"))

from sharepoint_enum import (  # noqa: E402
    GraphClient,
    INVENTORY_CSV_FIELDS,
    ScanState,
    build_parser,
    extract_text,
    print_scan_stats,
)


def test_enum_cli_requires_no_mode_or_acknowledgement_flags():
    args = build_parser().parse_args([])
    assert not hasattr(args, "metadata_only")
    assert not hasattr(args, "acknowledge_authorization")
    assert not hasattr(args, "access_token_file")
    assert not hasattr(args, "access_token_stdin")
    assert not hasattr(args, "sharepoint_cookie_file")
    assert not hasattr(args, "sharepoint_url")
    assert not hasattr(args, "rules")


def test_graph_url_rejects_non_graph_host():
    try:
        GraphClient.absolute_url("https://example.com/page")
    except ValueError as exc:
        assert "non-Microsoft" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_graph_client_refreshes_callable_token_after_401():
    class TokenSource:
        invalidated = False

        def __call__(self):
            return "new-token" if self.invalidated else "old-token"

        def invalidate(self):
            self.invalidated = True

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

    source = TokenSource()
    client = GraphClient(source, max_retries=1)
    statuses = iter([401, 200])
    authorization_headers = []

    def fake_request(*_args, **_kwargs):
        authorization_headers.append(client.session.headers["Authorization"])
        return Response(next(statuses))

    client.session.request = fake_request
    response = client.request("GET", "/me")

    assert response.status_code == 200
    assert authorization_headers == ["Bearer old-token", "Bearer new-token"]


def test_extract_docx_xml():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:p><w:t>hello secret</w:t></w:p>")
        archive.writestr("docProps/core.xml", "<x>ignored</x>")
    text = extract_text("sample.docx", buffer.getvalue())
    assert "hello secret" in text
    assert "ignored" not in text


def test_scan_state_prints_periodic_status(tmp_path, capsys):
    state = ScanState(
        output=tmp_path / "scan",
        status_every=2,
    )
    try:
        state.write("inventory", {"name": "one.txt", "size": 1})
        assert "[*] Status:" not in capsys.readouterr().out
        state.write("inventory", {"name": "two.txt", "size": 2})
        output = capsys.readouterr().out
        assert "files=2" in output
        assert "rate=" in output
    finally:
        state.close()


def test_offline_scan_stats(tmp_path, capsys):
    scan = tmp_path / "scan-test"
    scan.mkdir()
    for name in ("sites", "drives", "findings", "errors"):
        (scan / f"{name}.jsonl").write_text("{}\n")
    with (scan / "inventory.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INVENTORY_CSV_FIELDS)
        writer.writeheader()
        writer.writerow({
            "ID": "1", "Site": "Site A", "Library": "Docs",
            "Path": "folder/report.pdf", "Name": "report.pdf",
            "SizeMB": "0.001", "SizeBytes": "1024", "Modified": "",
            "WebURL": "https://example.invalid/report.pdf",
            "DriveID": "drive", "ItemID": "item", "MimeType": "application/pdf",
            "SharedScope": "",
        })
    (scan / "summary.json").write_text(json.dumps({
        "authentication": "supplied-graph-token",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:02+00:00",
        "exit_code": 0,
    }))
    assert print_scan_stats(str(scan)) == 0
    output = capsys.readouterr().out
    assert "Status:         complete" in output
    assert "Files:          1" in output
    assert "Total size:     1.00 KiB" in output
    assert "Site A/Docs" in output
