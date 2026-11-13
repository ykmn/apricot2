"""Read/write helpers for GUI-based YAML config editing.

Stations and playlogs are stored one ``id`` per file in config/stations/ and
config/playlogs/.  These helpers resolve id ↔ filename, validate payloads and
write YAML atomically (tempfile + os.replace), so a crash mid-write cannot
truncate a config file.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR   = Path(__file__).parent.parent / "config"
STATIONS_DIR = CONFIG_DIR / "stations"
PLAYLOGS_DIR = CONFIG_DIR / "playlogs"
LDAP_YAML    = CONFIG_DIR / "ldap.yaml"
USERS_YAML   = CONFIG_DIR / "users.yaml"
SETTINGS_YAML = CONFIG_DIR / "settings.yaml"
SECRET_YAML  = CONFIG_DIR / "secret.yaml"

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ConfigStoreError(ValueError):
    """User-visible validation or IO error in a config write."""


def read_yaml(path: Path) -> Any:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            with path.open(encoding=enc) as fh:
                return yaml.safe_load(fh)
        except UnicodeDecodeError:
            continue
        except yaml.YAMLError as exc:
            raise ConfigStoreError(f"{path.name}: ошибка синтаксиса YAML — {exc}") from exc
    raise ConfigStoreError(f"{path.name}: не удалось прочитать файл (кодировка)")


def _dump(path_or_fd, data: Any) -> None:
    yaml.safe_dump(data, path_or_fd, allow_unicode=True, sort_keys=False,
                   default_flow_style=False, width=120)


def write_yaml(path: Path, data: Any) -> None:
    """Write YAML, UTF-8, preserving key order.

    Atomic (tempfile + os.replace) when possible.  On Windows some processes
    hold config files open without delete sharing, which blocks os.replace —
    retry a few times, then fall back to a direct overwrite.
    """
    import time
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=path.stem + "_")
    try:
        with open(fd, "w", encoding="utf-8") as fh:
            _dump(fh, data)
        for attempt in range(3):
            try:
                os.replace(tmp, str(path))
                return
            except PermissionError:
                if attempt < 2:
                    time.sleep(0.5)
        # Fallback: non-atomic direct write (works when the file is held
        # open with read+write sharing, which is the common Windows case).
        try:
            with path.open("w", encoding="utf-8") as fh:
                _dump(fh, data)
            print(f"[config_store] WARNING: {path.name}: atomic replace blocked, "
                  f"used direct overwrite")
        except PermissionError as exc:
            raise ConfigStoreError(
                f"{path.name}: файл занят другим процессом "
                f"(закройте его в редакторе / остановите второй сервер)"
            ) from exc
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def validate_id(id_: Any, what: str = "id") -> str:
    if not isinstance(id_, str) or not _ID_RE.match(id_):
        raise ConfigStoreError(
            f"{what}: допустимы только латинские буквы, цифры, «-» и «_» (1–64 символа)"
        )
    return id_


def scan_ids(directory: Path) -> dict[str, Path]:
    """Map top-level ``id:`` → file for every YAML file in *directory*."""
    result: dict[str, Path] = {}
    if not directory.exists():
        return result
    for f in sorted(directory.glob("*.yaml")):
        try:
            raw = read_yaml(f)
        except ConfigStoreError:
            continue
        if isinstance(raw, dict) and isinstance(raw.get("id"), str):
            result[raw["id"]] = f
    return result


def list_raw(directory: Path) -> list[dict]:
    """Return raw dicts of all YAML files in *directory* (skip broken ones)."""
    items: list[dict] = []
    if not directory.exists():
        return items
    for f in sorted(directory.glob("*.yaml")):
        try:
            raw = read_yaml(f)
        except ConfigStoreError:
            continue
        if isinstance(raw, dict):
            items.append(raw)
    return items


def save_by_id(directory: Path, old_id: str | None, data: dict) -> str:
    """Write *data* (must contain ``id``) to the file owning that id.

    ``old_id`` identifies the existing record on rename; the old file is
    removed when the id changed.  Returns the new id.
    """
    new_id = validate_id(data.get("id"))
    if not data.get("name"):
        raise ConfigStoreError("Поле name обязательно")
    files = scan_ids(directory)
    if new_id in files and new_id != old_id:
        raise ConfigStoreError(f"id «{new_id}» уже существует")
    if old_id is not None and old_id not in files:
        raise ConfigStoreError(f"Запись «{old_id}» не найдена")

    directory.mkdir(parents=True, exist_ok=True)
    target = files.get(old_id) if old_id else None
    if target is None or target.stem != new_id:
        # New record or id changed → canonical filename <id>.yaml
        target = directory / f"{new_id}.yaml"
    write_yaml(target, data)
    if old_id and old_id != new_id:
        old_file = files[old_id]
        if old_file != target and old_file.exists():
            old_file.unlink()
    return new_id


def delete_by_id(directory: Path, id_: str) -> None:
    files = scan_ids(directory)
    f = files.get(id_)
    if f is None:
        raise ConfigStoreError(f"Запись «{id_}» не найдена")
    f.unlink()


def secrets_list() -> list[dict]:
    """Secret entries without passwords — for dropdowns in the GUI."""
    if not SECRET_YAML.exists():
        return []
    data = read_yaml(SECRET_YAML) or {}
    return [
        {"id": e.get("id"), "username": e.get("username", ""), "domain": e.get("domain", "")}
        for e in data.get("authorization", [])
        if isinstance(e, dict) and "id" in e
    ]
