import json
from os import environ
from pathlib import Path

from types import UnionType
from typing import get_args, get_origin
from dataclasses import dataclass, field, fields, is_dataclass, MISSING
from ehclone.db.entities import Category


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
class EHExtraArgs:
    require_torrents:        bool = False
    disable_language_filter: bool = False
    disable_uploader_filter: bool = False
    disable_tags_filter:     bool = False
    min_rating: int | None = None
    min_pages:  int | None = None
    max_pages:  int | None = None

    def to_dict(self):
        args = {}

        for k, v in EHExtraArgs._BOOL_PARAM_DICT.items():
            value = getattr(self, k)
            if value:
                args[v] = 'on'
        for k, v in EHExtraArgs._INT_PARAM_DICT.items():
            value = getattr(self, k)
            if value is not None:
                args[v] = value
        return args

EHExtraArgs._BOOL_PARAM_DICT = {
    'require_torrents':        'f_sto',
    'min_rating':              'f_srdd',
    'disable_language_filter': 'f_sfl',
    'disable_uploader_filter': 'f_sfu',
    'disable_tags_filter':     'f_sft',
}
EHExtraArgs._INT_PARAM_DICT = {
    'min_rating': 'f_srdd',
    'min_pages':  'f_spf',
    'max_pages':  'f_spt',
}


@dataclass
class EH:
    base_url: str = 'https://e-hentai.org'
    proxy: str | None = None
    cookies: dict[str, str] = field(default_factory=dict)
    categories: list[Category] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    include_expunged: bool = True
    extra_args: EHExtraArgs = field(default_factory=EHExtraArgs)
    min_request_interval: int = 5
    torrent_key: str | None = None

    def get_f_cats(self):
        disabled_sum = 1023
        for cat in self.categories:
            disabled_sum -= EH._CATEGORY_ENCODINGS[cat]
        return disabled_sum

    def get_search_args(self):
        args = {}
        if self.keywords:
            args['f_search'] = ' '.join(self.keywords)
        args['f_cats'] = self.get_f_cats()
        args.update(self.extra_args.to_dict())
        return args


EH._CATEGORY_ENCODINGS = {
    Category.MISC:       1,
    Category.DOUJINSHI:  2,
    Category.MANGA:      4,
    Category.ARTIST_CG:  8,
    Category.GAME_CG:    16,
    Category.IMAGE_SET:  32,
    Category.COSPLAY:    64,
    Category.ASIAN_PORN: 128,
    Category.NON_H:      256,
    Category.WESTERN:    512,
}


@dataclass
class FilterDedupe:
    thumb_dir: Path
    enabled: bool = True
    page_tolerance: int = 8
    cosine_threshold: float = 0.1
    rating_factor: float = 1.0
    page_count_factor: float = 5.0
    expunged_tags: list[str] = field(default_factory=list)
    expunged_bias: int = -100
    tag_biases: dict[str, int] = field(default_factory=dict)

@dataclass
class Filter:
    dedupe: FilterDedupe
    not_before: int = 3600
    min_rating: int = 0
    wild_tags: list[str] = field(default_factory=list)


@dataclass
class Aria2:
    url: str | None
    token: str | None
    remote_dir: Path | None
    local_dir: Path | None
    task_limit: int = 128
    poll_interval: int = 10


@dataclass
class QBit:
    url: str | None
    username: str | None
    password: str | None
    remote_dir: Path | None
    local_dir: Path | None
    enabled: bool = True
    category: str | None = None
    task_limit: int = 1024
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
    filter: Filter
    aria2: Aria2
    qbit: QBit
    eh: EH = field(default_factory=EH)
    log: Log = field(default_factory=Log)
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
