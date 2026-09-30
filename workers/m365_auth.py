"""Authentication helpers for read-only Microsoft Graph tooling."""

from __future__ import annotations

import argparse
import base64
import configparser
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from m365_output import info, warning


GRAPH_AUDIENCES = {
    "00000003-0000-0000-c000-000000000000",
    "https://graph.microsoft.com",
    "https://graph.microsoft.com/",
}
ACCESS_TOKEN_ENV_NAME = "M365AT"
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
CLIENT_ID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
TENANT_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


@dataclass
class RefreshCredentials:
    path: Path
    section: str
    refresh_token: str
    client_id: str
    domain: str
    access_token: str = ""


def auth_status(message: str) -> None:
    info(f"Auth: {message}", file=sys.stderr)


def token_expiry_text(token: str) -> str:
    expires = float(jwt_claims_unverified(token)["exp"])
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(expires))


def jwt_claims_unverified(token: str) -> dict[str, Any]:
    """Decode claims for safety checks; this does not authenticate the JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("access token is not a JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("access token has an invalid JWT payload") from exc
    if not isinstance(value, dict):
        raise ValueError("access token JWT payload is not an object")
    return value


def inspect_supplied_token(token: str) -> str:
    token = token.strip()
    if not token:
        raise ValueError("access token is empty")
    claims = jwt_claims_unverified(token)
    expires = claims.get("exp")
    if not isinstance(expires, (int, float)):
        raise ValueError("access token has no numeric exp claim")
    if expires <= time.time() + 30:
        raise ValueError("access token is expired or expires within 30 seconds")
    audience = claims.get("aud")
    if audience not in GRAPH_AUDIENCES:
        raise ValueError(f"token audience is not Microsoft Graph (aud={audience!r})")
    return token


def access_token_from_environment() -> str | None:
    return os.environ.get(ACCESS_TOKEN_ENV_NAME) or None


def _config_parser() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    return parser


def _config_value(values: configparser.SectionProxy, name: str) -> str:
    for key, value in values.items():
        if key.casefold() == name.casefold():
            return value.strip()
    return ""


def read_refresh_credentials(path: Path, section: str = "DEFAULT") -> RefreshCredentials:
    try:
        if path.stat().st_mode & 0o077:
            warning(f"credential file {path} is readable by group/others; use chmod 600.")
    except OSError:
        pass

    config = _config_parser()
    try:
        loaded = config.read(path, encoding="utf-8")
    except configparser.Error as exc:
        raise ValueError(f"could not parse credential file {path}: {exc}") from exc
    if not loaded:
        raise ValueError(f"credential file not found: {path}")
    if section != config.default_section and section not in config:
        raise ValueError(f"section {section!r} not found in credential file {path}")

    values = config[section]
    refresh_token = _config_value(values, "REFRESHTOKEN")
    client_id = _config_value(values, "CLIENT_ID")
    domain = _config_value(values, "DOMAIN")
    access_token = _config_value(values, "ACCESSTOKEN")
    missing = [name for name, value in (
        ("REFRESHTOKEN", refresh_token), ("CLIENT_ID", client_id), ("DOMAIN", domain)
    ) if not value]
    if missing:
        raise ValueError(f"{', '.join(missing)} missing in section {section!r} of {path}")
    if not CLIENT_ID_PATTERN.fullmatch(client_id):
        raise ValueError(f"CLIENT_ID in section {section!r} must be an application GUID")
    if not TENANT_PATTERN.fullmatch(domain) or ".." in domain:
        raise ValueError(
            f"DOMAIN in section {section!r} must be a tenant GUID or tenant domain"
        )
    return RefreshCredentials(path, section, refresh_token, client_id, domain, access_token)


def _set_config_value(
    values: configparser.SectionProxy, name: str, value: str
) -> None:
    key = next((key for key in values if key.casefold() == name.casefold()), name)
    values[key] = value


def _update_cached_tokens(
    credentials: RefreshCredentials, access_token: str, refresh_token: str
) -> None:
    config = _config_parser()
    if not config.read(credentials.path, encoding="utf-8"):
        raise ValueError(f"credential file not found: {credentials.path}")
    values = config[credentials.section]
    _set_config_value(values, "ACCESSTOKEN", access_token)
    _set_config_value(values, "REFRESHTOKEN", refresh_token)

    credentials.path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{credentials.path.name}.", dir=credentials.path.parent, text=True
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            config.write(handle)
        os.replace(temporary_path, credentials.path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


class RefreshTokenProvider:
    def __init__(self, config_path: Path, section: str = "DEFAULT", timeout: int = 60):
        auth_status(f"reading credentials from {config_path} [{section}]")
        self.credentials = read_refresh_credentials(config_path, section)
        self.timeout = timeout
        self._access_token: str | None = None
        self._expires_at = 0.0
        if self.credentials.access_token:
            try:
                self._access_token = inspect_supplied_token(self.credentials.access_token)
                expires = float(jwt_claims_unverified(self._access_token)["exp"])
                self._expires_at = time.monotonic() + max(0.0, expires - time.time())
                if self._expires_at > time.monotonic() + 60:
                    auth_status(
                        f"using cached Graph access token; expires {token_expiry_text(self._access_token)}"
                    )
                else:
                    auth_status("cached access token expires soon; refreshing it")
                    self._access_token = None
                    self._expires_at = 0.0
            except (KeyError, TypeError, ValueError):
                auth_status("cached access token is invalid or expired; refreshing it")
                self._access_token = None
                self._expires_at = 0.0
        else:
            auth_status("no cached access token found; using the refresh token")

    def invalidate(self) -> None:
        auth_status("Graph rejected the access token; refreshing it before retry")
        self._access_token = None
        self._expires_at = 0.0

    def __call__(self) -> str:
        if self._access_token and self._expires_at > time.monotonic() + 60:
            return self._access_token
        if self._access_token:
            auth_status("access token expires soon; requesting a new one")
            self._access_token = None
            self._expires_at = 0.0

        endpoint = (
            f"https://login.microsoftonline.com/{self.credentials.domain}/oauth2/v2.0/token"
        )
        auth_status(
            f"requesting a Graph access token for tenant {self.credentials.domain}"
        )
        try:
            response = requests.post(
                endpoint,
                data={
                    "client_id": self.credentials.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": self.credentials.refresh_token,
                    "scope": GRAPH_DEFAULT_SCOPE,
                },
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"refresh-token request failed: {exc}") from exc

        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"refresh-token request returned HTTP {response.status_code} with invalid JSON"
            ) from exc
        if not response.ok:
            error = str(result.get("error") or "token_error")
            description = str(result.get("error_description") or "no description")
            raise RuntimeError(
                f"refresh-token request failed (HTTP {response.status_code}): "
                f"{error}: {description[:500]}"
            )

        token = inspect_supplied_token(str(result.get("access_token") or ""))
        try:
            expires_in = max(60, int(result.get("expires_in", 3600)))
        except (TypeError, ValueError):
            expires_in = 3600
        self._access_token = token
        self._expires_at = time.monotonic() + expires_in
        auth_status(f"received Graph access token; expires {token_expiry_text(token)}")

        rotated_token = str(result.get("refresh_token") or "").strip()
        current_refresh_token = rotated_token or self.credentials.refresh_token
        try:
            _update_cached_tokens(self.credentials, token, current_refresh_token)
        except (OSError, ValueError, configparser.Error, KeyError) as exc:
            warning(f"could not cache tokens in {self.credentials.path}: {exc}")
        else:
            cached = "access token and rotated refresh token" if rotated_token else "access token"
            auth_status(f"cached {cached} in {self.credentials.path} [{self.credentials.section}]")
        self.credentials.access_token = token
        self.credentials.refresh_token = current_refresh_token
        return token


def refresh_credentials_available(args: argparse.Namespace) -> bool:
    path = getattr(args, "config", None)
    return bool(path and Path(path).is_file())


def resolve_token_source(args: argparse.Namespace) -> str | Callable[[], str]:
    token = access_token_from_environment()
    if token:
        token = inspect_supplied_token(token)
        auth_status(
            f"using static Graph access token from M365AT; expires {token_expiry_text(token)}; "
            "automatic refresh unavailable"
        )
        return token
    config_path = getattr(args, "config", None)
    if config_path:
        return RefreshTokenProvider(
            Path(config_path),
            getattr(args, "section", "DEFAULT"),
            getattr(args, "timeout", 60),
        )
    raise ValueError(
        "set M365AT or provide a refresh-token credential file"
    )
