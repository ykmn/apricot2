"""Load and validate YAML configuration files."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def _open_yaml(path: Path) -> Any:
    """Open and parse a YAML file.

    Tries UTF-8 with BOM first (handles files saved by Windows editors).
    On UnicodeDecodeError falls back to cp1251 with a warning, then raises
    a clear ConfigError pointing to the offending file.
    """
    for enc in ("utf-8-sig", "cp1251"):
        try:
            with path.open(encoding=enc) as fh:
                return yaml.safe_load(fh)
        except UnicodeDecodeError:
            continue
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path.name}: YAML syntax error — {exc}") from exc
    raise ConfigError(
        f"{path.name}: не удалось прочитать файл — "
        f"сохраните его как UTF-8 (без BOM или с BOM)"
    )


class ConfigError(ValueError):
    """Raised when a config file has invalid syntax or encoding."""

from .models import (
    ChannelConfig, PlaylistConfig, PlaylistSource, SMBConfig, StationConfig,
)

CONFIG_DIR = Path(__file__).parent.parent / "config"

# Cached secrets indexed by id
_secrets: dict[int, dict] | None = None


def _load_secrets() -> dict[int, dict]:
    global _secrets
    if _secrets is not None:
        return _secrets
    secret_path = CONFIG_DIR / "secret.yaml"
    if not secret_path.exists():
        _secrets = {}
        return _secrets
    data = _open_yaml(secret_path) or {}
    _secrets = {
        entry["id"]: entry
        for entry in data.get("authorization", [])
        if isinstance(entry, dict) and "id" in entry
    }
    return _secrets


def invalidate_secrets_cache() -> None:
    """Force the next _load_secrets() call to re-read config/secret.yaml from disk."""
    global _secrets
    _secrets = None


def _resolve_password(raw: dict) -> str | None:
    """Resolve password: env var → plaintext field → None."""
    if env_key := raw.get("password_env"):
        val = os.environ.get(env_key)
        if val is not None:
            return val
        print(f"[config] Info: env var {env_key!r} not set, trying plaintext 'password' field")
    return raw.get("password")


def _parse_smb(raw: dict | None) -> SMBConfig | None:
    if not raw:
        return None
    creds: dict = {}
    if secret_id := raw.get("secret"):
        secrets = _load_secrets()
        if secret_id not in secrets:
            raise ValueError(f"[config] secret id={secret_id} not found in config/secret.yaml")
        creds = secrets[secret_id]
    return SMBConfig(
        host=raw["host"],
        share=raw["share"],
        path=raw.get("path", ""),
        username=creds.get("username") or raw.get("username", ""),
        password=creds.get("password") or _resolve_password(raw),
        password_env=raw.get("password_env"),
        domain=creds.get("domain") or raw.get("domain"),
        # auth_protocol: secret overrides smb-block (more specific wins)
        auth_protocol=creds.get("auth_protocol") or raw.get("auth_protocol"),
    )


def _parse_channel(raw: dict) -> ChannelConfig:
    return ChannelConfig(
        id=raw["id"],
        name=raw["name"],
        folder_format=raw.get("folder_format", "%Y-%m-%d"),
        file_format=raw.get("file_format", "%H-%M-%S"),
        file_extension=raw.get("file_extension", "wav").lstrip("."),
        sample_rate=int(raw.get("sample_rate", 44100)),
        bitrate=raw.get("bitrate"),
        local_path=raw.get("local_path"),
        smb=_parse_smb(raw.get("smb")),
        playlogs=raw.get("playlogs", []),
        playlogs_offset=int(raw.get("playlogs_offset", 0)),
    )


def load_stations() -> dict[str, StationConfig]:
    stations: dict[str, StationConfig] = {}
    station_dir = CONFIG_DIR / "stations"
    if not station_dir.exists():
        return stations
    for f in sorted(station_dir.glob("*.yaml")):
        raw: dict[str, Any] = _open_yaml(f)
        station = StationConfig(
            id=raw["id"],
            name=raw["name"],
            channels=[_parse_channel(c) for c in raw.get("channels", [])],
        )
        stations[station.id] = station
    return stations


def _parse_playlist_source(raw: dict, idx: int) -> PlaylistSource:
    return PlaylistSource(
        priority=int(raw.get("priority", idx + 1)),
        file_mask=raw.get("file_mask", "%Y-%m-%d.log"),
        encoding=raw.get("encoding", "windows-1251"),
        delimiter=raw.get("delimiter", ","),
        header_skip_prefix=raw.get("header_skip_prefix", "FIELD LIST"),
        local_path=raw.get("local_path"),
        smb=_parse_smb(raw.get("smb")),
    )


def load_playlists() -> dict[str, PlaylistConfig]:
    playlists: dict[str, PlaylistConfig] = {}
    pl_dir = CONFIG_DIR / "playlogs"
    if not pl_dir.exists():
        return playlists
    for f in sorted(pl_dir.glob("*.yaml")):
        raw: dict[str, Any] = _open_yaml(f)
        sources = [
            _parse_playlist_source(s, i)
            for i, s in enumerate(raw.get("sources", []))
        ]
        sources.sort(key=lambda s: s.priority)
        pl = PlaylistConfig(
            id=raw["id"],
            name=raw["name"],
            sources=sources,
            fields=raw.get("fields", {}),
            class_colors=raw.get("class_colors", {}),
            class_names=raw.get("class_names", {}),
        )
        playlists[pl.id] = pl
    return playlists


def load_settings() -> dict[str, Any]:
    settings_path = CONFIG_DIR / "settings.yaml"
    if not settings_path.exists():
        return {}
    return _open_yaml(settings_path) or {}
