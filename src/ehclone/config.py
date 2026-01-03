
import json
from os import environ
from pathlib import Path

from types import UnionType
from typing import get_args, get_origin
from dataclasses import dataclass, field, fields, is_dataclass, MISSING

APPDATA = Path(environ.get('APPDATA', '/appdata'))


@dataclass
class Log:
    console_level: str = 'INFO'
    file_level: str = 'WARNING'
    dir: Path | None = APPDATA / 'logs'


@dataclass
class DB:
    url: str


@dataclass
class EH:
    base_url: str = 'https://e-hentai.org'
    proxy: str | None = None
    cookies: dict[str, str] = field(default_factory=dict)
    search_args: dict[str, str] = field(default_factory=dict)
    include_expunged: bool = True
    min_request_interval: int = 5
    torrent_key: str | None = None


@dataclass
class DedupeRanking:
    rating_factor: float = 1.0
    page_count_factor: float = 5.0
    expunged_tags: list[str] = field(default_factory=list)
    expunged_bias: int = -100
    tag_biases: dict[str, int] = field(default_factory=dict)


@dataclass
class Dedupe:
    thumb_dir: Path | None
    enabled: bool = True
    page_tolerance: int = 8
    cosine_threshold: float = 0.9
    not_before: int = 30 * 60
    ranking: DedupeRanking = field(default_factory=DedupeRanking)


@dataclass
class DownloadManager:
    poll_interval: int = 10 * 60
    categories: list[str] = field(default_factory=list)
    min_rating: int = 0


@dataclass
class Aria2:
    url: str | None
    token: str | None
    remote_dir: Path | None
    local_dir: Path | None
    enabled: bool = True
    task_limit: int = 64


@dataclass
class QBit:
    url: str | None
    username: str | None
    password: str | None
    remote_dir: Path | None
    local_dir: Path | None
    enabled: bool = True
    category: str | None = None
    task_limit: int = 512
    timeout_hours: int = 24 * 7


@dataclass
class ArchiverBlacklist:
    sha1: list[str] = field(default_factory=list)
    qr: list[str] = field(default_factory=list)

@dataclass
class Archiver:
    cmd: list[str] = field(default_factory=list)
    poll_interval_hours: int = 24
    not_before_hours: int = 24 * 7
    blacklist: ArchiverBlacklist = field(default_factory=ArchiverBlacklist)


@dataclass
class Config:
    db: DB
    dedupe: Dedupe
    aria2: Aria2
    qbit: QBit
    eh: EH = field(default_factory=EH)
    log: Log = field(default_factory=Log)
    download_manager: DownloadManager = field(default_factory=DownloadManager)
    archiver: Archiver = field(default_factory=Archiver)


def load_dict(cls, data):
    cls_fields = fields(cls)

    enabled = True
    if (
        any(f.name == 'enabled' for f in cls_fields)
        and isinstance(data, dict)
        and data.get('enabled') is False
    ):
        enabled = False

    kwargs = {}
    for f in cls_fields:
        if isinstance(data, dict) and f.name in data:
            value = data[f.name]
            if is_dataclass(f.type):
                value = load_dict(f.type, value)
            elif value is not None:
                _f_type = f.type
                _origin = get_origin(f.type)
                if _origin is UnionType:
                    _f_type = get_args(_f_type)[0]
                    _origin = get_origin(_f_type)
                if _origin is None:
                    value = _f_type(value)
                else:
                    value = _origin(value)
            kwargs[f.name] = value
        else:
            print(f'WARNING: {cls.__name__}.{f.name} missing from config')
            if f.default_factory is not MISSING:
                kwargs[f.name] = f.default_factory()
            elif f.default is not MISSING:
                kwargs[f.name] = f.default
            else:
                if not enabled:
                    kwargs[f.name] = None
                else:
                    raise ValueError(f'Missing required config field: {cls.__name__}.{f.name}')

    return cls(**kwargs)


def load_config():
    config_path = APPDATA / 'config.json'
    with open(config_path) as f:
        raw_config = json.load(f)
    return load_dict(Config, raw_config)


config = load_config()


if __name__ == '__main__':
    from pprint import pprint
    pprint(config)
