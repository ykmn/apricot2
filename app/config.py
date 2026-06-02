"""Load and validate YAML configuration files."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .models import (
    ChannelConfig, PlaylistConfig, SMBConfig, StationConfig,
)

CONFIG_DIR = Path(__file__).parent.parent / "config"


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
    return SMBConfig(
        host=raw["host"],
        share=raw["share"],
        path=raw.get("path", ""),
        username=raw.get("username", ""),
        password=_resolve_password(raw),
        password_env=raw.get("password_env"),
        domain=raw.get("domain"),
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
        playlists=raw.get("playlists", []),
    )


def load_stations() -> dict[str, StationConfig]:
    stations: dict[str, StationConfig] = {}
    station_dir = CONFIG_DIR / "stations"
    if not station_dir.exists():
        return stations
    for f in sorted(station_dir.glob("*.yaml")):
        with f.open(encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh)
        station = StationConfig(
            id=raw["id"],
            name=raw["name"],
            channels=[_parse_channel(c) for c in raw.get("channels", [])],
        )
        stations[station.id] = station
    return stations


def load_playlists() -> dict[str, PlaylistConfig]:
    playlists: dict[str, PlaylistConfig] = {}
    pl_dir = CONFIG_DIR / "playlists"
    if not pl_dir.exists():
        return playlists
    for f in sorted(pl_dir.glob("*.yaml")):
        with f.open(encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh)
        pl = PlaylistConfig(
            id=raw["id"],
            name=raw["name"],
            file_mask=raw.get("file_mask", "%Y-%m-%d.csv"),
            encoding=raw.get("encoding", "utf-8-sig"),
            delimiter=raw.get("delimiter", ";"),
            fields=raw.get("fields", {}),
            class_colors=raw.get("class_colors", {}),
            class_names=raw.get("class_names", {}),
            local_path=raw.get("local_path"),
            smb=_parse_smb(raw.get("smb")),
        )
        playlists[pl.id] = pl
    return playlists


def load_settings() -> dict[str, Any]:
    settings_path = CONFIG_DIR / "settings.yaml"
    if not settings_path.exists():
        return {}
    with settings_path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}
