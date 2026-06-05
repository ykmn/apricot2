from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class SMBConfig:
    host: str
    share: str
    path: str = ""
    username: str = ""
    password: Optional[str] = None
    password_env: Optional[str] = None
    domain: Optional[str] = None
    auth_protocol: Optional[str] = None   # "ntlm" | "kerberos" | None (auto)


@dataclass
class ChannelConfig:
    id: str
    name: str
    folder_format: str = "%Y-%m-%d"
    file_format: str = "%H-%M-%S"
    file_extension: str = "wav"
    sample_rate: int = 44100
    bitrate: Optional[str] = None
    local_path: Optional[str] = None
    smb: Optional[SMBConfig] = None
    playlogs: list[str] = field(default_factory=list)


@dataclass
class StationConfig:
    id: str
    name: str
    channels: list[ChannelConfig] = field(default_factory=list)


@dataclass
class PlaylistSource:
    priority: int
    file_mask: str = "%Y-%m-%d.log"
    encoding: str = "windows-1251"
    delimiter: str = ","
    header_skip_prefix: str = "FIELD LIST"  # first cell of header row to skip
    local_path: Optional[str] = None
    smb: Optional[SMBConfig] = None


@dataclass
class PlaylistConfig:
    id: str
    name: str
    sources: list = field(default_factory=list)   # list[PlaylistSource] sorted by priority
    fields: dict = field(default_factory=dict)
    class_colors: dict = field(default_factory=dict)
    class_names: dict = field(default_factory=dict)


@dataclass
class AudioFile:
    channel_id: str
    path: str          # full path (local or smb UNC)
    rel_path: str      # relative key used in index, e.g. "2024-06-03/18-00-00.mp3"
    start_dt: datetime
    end_dt: datetime
    duration: float    # seconds
    is_smb: bool = False


@dataclass
class PlaylistEntry:
    timestamp: datetime
    title: str
    cls: str
    duration: Optional[float] = None
    elem_id: str = ""


@dataclass
class LogItem:
    id: str
    channel_id: str
    channel_name: str
    start_time: datetime
    end_time: datetime
    label: str = ""
