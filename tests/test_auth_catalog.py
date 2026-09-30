import argparse
import base64
import csv
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workers"))

from m365_auth import (
    GRAPH_DEFAULT_SCOPE,
    RefreshTokenProvider,
    inspect_supplied_token,
    read_refresh_credentials,
    resolve_token_source,
)
from m365_catalog import (
    ALL_GROUP_KEY,
    FIELDS,
    command_catalog,
    command_groups,
    command_sum,
    format_size_mb,
)


def fake_jwt(**claims):
    header = {"alg": "none"}
    body = {
        "aud": "00000003-0000-0000-c000-000000000000",
        "exp": time.time() + 3600,
        **claims,
    }

    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{encode(header)}.{encode(body)}.signature"


def test_accepts_unexpired_graph_token():
    token = fake_jwt()
    assert inspect_supplied_token(token) == token


def test_rejects_wrong_audience():
    try:
        inspect_supplied_token(fake_jwt(aud="https://management.azure.com/"))
    except ValueError as exc:
        assert "audience" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_resolve_access_token_requires_user_supplied_token(monkeypatch):
    monkeypatch.delenv("M365AT", raising=False)
    args = argparse.Namespace()
    try:
        resolve_token_source(args)
    except ValueError as exc:
        assert "M365AT" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_resolve_access_token_reads_m365at_environment(monkeypatch):
    token = fake_jwt()
    monkeypatch.setenv("M365AT", token)
    args = argparse.Namespace()
    assert resolve_token_source(args) == token


def test_refresh_token_provider_uses_graph_default_and_rotates_file(
    tmp_path, monkeypatch, capsys
):
    token = fake_jwt()
    expired_token = fake_jwt(exp=time.time() - 60)
    path = tmp_path / "creds.ini"
    path.write_text(
        "[DEFAULT]\n"
        "REFRESHTOKEN=old-refresh-token\n"
        "CLIENT_ID=11111111-2222-3333-4444-555555555555\n"
        "DOMAIN=contoso.onmicrosoft.com\n"
        f"ACCESSTOKEN={expired_token}\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    calls = []

    class Response:
        ok = True
        status_code = 200

        @staticmethod
        def json():
            return {
                "access_token": token,
                "refresh_token": "rotated-refresh-token",
                "expires_in": 3600,
            }

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("m365_auth.requests.post", fake_post)
    provider = RefreshTokenProvider(path, timeout=12)

    assert provider() == token
    assert provider() == token
    assert len(calls) == 1
    assert calls[0][0].endswith(
        "/contoso.onmicrosoft.com/oauth2/v2.0/token"
    )
    assert calls[0][1]["data"]["scope"] == GRAPH_DEFAULT_SCOPE
    assert calls[0][1]["timeout"] == 12
    updated = read_refresh_credentials(path)
    assert updated.access_token == token
    assert updated.refresh_token == "rotated-refresh-token"
    assert path.stat().st_mode & 0o777 == 0o600
    status = capsys.readouterr().err
    assert "cached access token is invalid or expired" in status
    assert "requesting a Graph access token" in status
    assert "received Graph access token" in status
    assert "cached access token and rotated refresh token" in status


def test_refresh_token_provider_reuses_valid_cached_access_token(
    tmp_path, monkeypatch, capsys
):
    token = fake_jwt()
    path = tmp_path / "creds.ini"
    path.write_text(
        "[DEFAULT]\n"
        "REFRESHTOKEN=refresh-token\n"
        "CLIENT_ID=11111111-2222-3333-4444-555555555555\n"
        "DOMAIN=contoso.onmicrosoft.com\n"
        f"ACCESSTOKEN={token}\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    def unexpected_post(*_args, **_kwargs):
        raise AssertionError("valid cached access token should not be refreshed")

    monkeypatch.setattr("m365_auth.requests.post", unexpected_post)
    assert RefreshTokenProvider(path)() == token
    status = capsys.readouterr().err
    assert "reading credentials" in status
    assert "using cached Graph access token" in status
    assert "expires" in status


def test_refresh_token_provider_reports_refresh_failure(tmp_path, monkeypatch):
    path = tmp_path / "creds.ini"
    path.write_text(
        "[DEFAULT]\n"
        "REFRESHTOKEN=invalid-refresh-token\n"
        "CLIENT_ID=11111111-2222-3333-4444-555555555555\n"
        "DOMAIN=contoso.onmicrosoft.com\n"
        "ACCESSTOKEN=expired-or-invalid\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    class Response:
        ok = False
        status_code = 400

        @staticmethod
        def json():
            return {
                "error": "invalid_grant",
                "error_description": "The refresh token is invalid or expired.",
            }

    monkeypatch.setattr("m365_auth.requests.post", lambda *_args, **_kwargs: Response())

    try:
        RefreshTokenProvider(path)()
    except RuntimeError as exc:
        assert "invalid_grant" in str(exc)
        assert "invalid or expired" in str(exc)
    else:
        raise AssertionError("expected refresh failure")


def test_refresh_token_provider_renews_during_long_running_process(tmp_path, monkeypatch):
    first_token = fake_jwt(token_number=1)
    second_token = fake_jwt(token_number=2)
    path = tmp_path / "creds.ini"
    path.write_text(
        "[DEFAULT]\n"
        "REFRESHTOKEN=initial-refresh-token\n"
        "CLIENT_ID=11111111-2222-3333-4444-555555555555\n"
        "DOMAIN=contoso.onmicrosoft.com\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    responses = iter([
        (first_token, "rotated-refresh-token-1"),
        (second_token, "rotated-refresh-token-2"),
    ])
    calls = []

    class Response:
        ok = True
        status_code = 200

        def __init__(self, access_token, refresh_token):
            self.access_token = access_token
            self.refresh_token = refresh_token

        def json(self):
            return {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_in": 3600,
            }

    def fake_post(*_args, **kwargs):
        calls.append(kwargs["data"]["refresh_token"])
        return Response(*next(responses))

    monkeypatch.setattr("m365_auth.requests.post", fake_post)
    provider = RefreshTokenProvider(path)

    assert provider() == first_token
    provider._expires_at = time.monotonic() + 30
    assert provider() == second_token
    assert calls == ["initial-refresh-token", "rotated-refresh-token-1"]
    updated = read_refresh_credentials(path)
    assert updated.access_token == second_token
    assert updated.refresh_token == "rotated-refresh-token-2"


def test_refresh_credentials_accept_tenant_guid(tmp_path):
    path = tmp_path / "creds.ini"
    tenant_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    path.write_text(
        "[DEFAULT]\n"
        "REFRESHTOKEN=refresh-token\n"
        "CLIENT_ID=11111111-2222-3333-4444-555555555555\n"
        f"DOMAIN={tenant_id}\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    assert read_refresh_credentials(path).domain == tenant_id


def test_catalog_preserves_csv_commas(tmp_path, capsys):
    path = tmp_path / "inventory.csv"
    row = ["1", "Site, East", "Documents", "folder/report,final.txt", "report,final.txt",
           "0.001", "100", "2026-01-01", "https://example.invalid/file", "drive", "item",
           "text/plain", ""]
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDS)
        writer.writerow(row)
    assert command_catalog(argparse.Namespace(csv=path)) == 0
    output = capsys.readouterr().out.rstrip("\n").split("\t")
    assert output[1] == "Site, East"
    assert output[3] == "folder/report,final.txt"


def test_group_picker_can_include_all_files(monkeypatch, capsys):
    rows = [
        ["1", "Site A", "Docs", "a.txt", "a.txt", "1.5", "1", "", "", "d1", "i1", "", ""],
        ["2", "Site B", "Docs", "b.txt", "b.txt", "2.5", "1", "", "", "d2", "i2", "", ""],
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO(
        "".join("\t".join(row) + "\n" for row in rows)
    ))
    args = argparse.Namespace(mode="site-library", include_all=True)

    assert command_groups(args) == 0
    first_line = capsys.readouterr().out.splitlines()[0]
    assert first_line == f"{ALL_GROUP_KEY}\t[All files] (2 files, 4.000 MB)"


def test_aggregate_size_uses_gigabytes_at_1024_mb():
    assert format_size_mb(1023.5) == "1023.500 MB"
    assert format_size_mb(1024) == "1.000 GB"
    assert format_size_mb(2560) == "2.500 GB"


def test_selected_rows_are_summed_for_fzf_preview(monkeypatch, capsys):
    rows = [
        ["1", "Site", "Docs", "a.bin", "a.bin", "1536", "1", "", "", "d", "i1", "", ""],
        ["2", "Site", "Docs", "b.bin", "b.bin", "1024", "1", "", "", "d", "i2", "", ""],
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO(
        "".join("\t".join(row) + "\n" for row in rows)
    ))

    assert command_sum(argparse.Namespace()) == 0
    assert capsys.readouterr().out.strip() == "Selected: 2 files, 2.500 GB"
