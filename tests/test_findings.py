import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workers"))

from m365_findings import item_findings, load_rules, main


def test_custom_rules_are_additive(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({
        "filename_terms": ["merger"],
        "sensitive_extensions": ["config"],
    }))

    terms, extensions = load_rules(path)

    assert {"password", "merger"} <= terms
    assert {".key", ".config"} <= extensions


def test_item_findings_match_name_and_extension():
    item = {
        "site_name": "Site A",
        "drive_name": "Documents",
        "item_id": "item-1",
        "name": "Payroll backup.PFX",
        "web_url": "https://contoso.sharepoint.com/report",
    }

    findings = list(item_findings(item, {"payroll", "secret"}, {".pfx"}))

    assert [finding["rule_id"] for finding in findings] == [
        "interesting-filename", "sensitive-extension"
    ]
    assert findings[0]["matched_term"] == "payroll"
    assert findings[1]["extension"] == ".pfx"


def test_findings_cli_consumes_scan_directory(tmp_path, monkeypatch):
    scan = tmp_path / "scan-test"
    scan.mkdir()
    rows = [
        {"name": "ordinary.txt", "item_id": "1"},
        {"name": "acquisition-plan.key", "item_id": "2"},
    ]
    (scan / "inventory.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    monkeypatch.setattr("sys.argv", [
        "m365_findings.py", str(scan), "--no-defaults",
        "--term", "acquisition", "--extension", "key",
    ])

    assert main() == 0

    findings = [
        json.loads(line)
        for line in (scan / "findings.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(findings) == 2
    assert {finding["rule_id"] for finding in findings} == {
        "interesting-filename", "sensitive-extension"
    }
    assert (scan / "findings.jsonl").stat().st_mode & 0o777 == 0o600
