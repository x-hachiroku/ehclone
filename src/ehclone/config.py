from os import environ
from pathlib import Path
from typing import ClassVar

import enum
from pydantic import BaseModel, Field, model_validator


APPDATA = Path(environ.get('APPDATA', '/appdata'))

class Category(str, enum.Enum):
    DOUJINSHI = 'Doujinshi'
    MANGA = 'Manga'
    ARTIST_CG = 'Artist CG'
    GAME_CG = 'Game CG'
    WESTERN = 'Western'
    IMAGE_SET = 'Image Set'
    NON_H = 'Non-H'
    COSPLAY = 'Cosplay'
    ASIAN_PORN = 'Asian Porn'
    MISC = 'Misc'


class Log(BaseModel):
    console_level: str = 'INFO'
    file_level: str = 'WARNING'
    dir: Path | None = APPDATA / 'logs'


class DB(BaseModel):
    url: str


class EHExtraArgs(BaseModel):
    require_torrents: bool = False
    disable_language_filter: bool = False
    disable_uploader_filter: bool = False
    disable_tags_filter: bool = False
    min_rating: int | None = None
    min_pages: int | None = None
    max_pages: int | None = None

    _BOOL_PARAM_DICT: ClassVar[dict[str, str]] = {
        'require_torrents': 'f_sto',
        'disable_language_filter': 'f_sfl',
        'disable_uploader_filter': 'f_sfu',
        'disable_tags_filter': 'f_sft',
    }
    _INT_PARAM_DICT: ClassVar[dict[str, str]] = {
        'min_rating': 'f_srdd',
        'min_pages': 'f_spf',
        'max_pages': 'f_spt',
    }

    def to_dict(self):
        args = {}

        for field_name, parameter in self._BOOL_PARAM_DICT.items():
            if getattr(self, field_name):
                args[parameter] = 'on'
        for field_name, parameter in self._INT_PARAM_DICT.items():
            value = getattr(self, field_name)
            if value is not None:
                args[parameter] = value
        return args


class EH(BaseModel):
    base_url: str = 'https://e-hentai.org'
    proxy: str | None = None
    cookies: dict[str, str] = Field(default_factory=dict)
    categories: list[Category] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    include_expunged: bool = True
    extra_args: EHExtraArgs = Field(default_factory=EHExtraArgs)
    min_request_interval: int = Field(default=5, ge=0)
    max_retry: int = Field(default=5, ge=0)
    torrent_key: str | None = None
    recheck: int = 0

    _CATEGORY_ENCODINGS: ClassVar[dict[Category, int]] = {
        Category.MISC: 1,
        Category.DOUJINSHI: 2,
        Category.MANGA: 4,
        Category.ARTIST_CG: 8,
        Category.GAME_CG: 16,
        Category.IMAGE_SET: 32,
        Category.COSPLAY: 64,
        Category.ASIAN_PORN: 128,
        Category.NON_H: 256,
        Category.WESTERN: 512,
    }

    def get_f_cats(self):
        if not self.categories:
            return 0
        disabled_sum = 1023
        for category in set(self.categories):
            disabled_sum -= self._CATEGORY_ENCODINGS[category]
        return disabled_sum

    def get_search_args(self):
        args = {}
        if self.keywords:
            args['f_search'] = ' '.join(self.keywords)
        args['f_cats'] = self.get_f_cats()
        args.update(self.extra_args.to_dict())
        return args


class FilterDedupe(BaseModel):
    thumb_dir: Path | None = APPDATA / 'thumbs'
    enabled: bool = False
    page_tolerance: int = Field(default=4, ge=0)
    page_tolerance_rate: float = Field(default=0.025, ge=0)
    page_tolerance_ad: int = Field(default=8, ge=0)
    distance_threshold: float = Field(default=0.05, ge=0, le=2)
    candidate_limit: int = Field(default=128, ge=1, le=500)
    rating_factor: float = 20.0
    page_count_factor: float = 5.0
    expunged_tags: list[str] = Field(default_factory=list)
    expunged_bias: int = -100
    tag_biases: dict[str, int] = Field(default_factory=dict)


class Filter(BaseModel):
    dedupe: FilterDedupe
    not_before_min: int = Field(default=60, ge=0)
    min_rating: float = Field(default=0, ge=0, le=5)
    include_categories: list[Category] = Field(default_factory=list)
    include_tags: list[str] = Field(default_factory=list)
    exclude_tags: list[str] = Field(default_factory=list)


class Aria2(BaseModel):
    url: str | None = None
    token: str | None = None
    remote_dir: Path | None = None
    local_dir: Path | None = None
    task_limit: int = 128

    @model_validator(mode='after')
    def require_download_paths(self):
        if self.url and (self.remote_dir is None or self.local_dir is None):
            raise ValueError('aria2 requires remote_dir and local_dir when enabled')
        return self


class QBit(BaseModel):
    url: str | None = None
    username: str | None = None
    password: str | None = None
    remote_dir: Path | None = None
    local_dir: Path | None = None
    enabled: bool = True
    category: str = ''
    task_limit: int = 256
    timeout_hours: int = Field(default=24 * 7, gt=0)

    @model_validator(mode='after')
    def require_connection(self):
        if self.enabled and (not self.url or self.remote_dir is None or self.local_dir is None):
            raise ValueError('qbit requires url, remote_dir and local_dir when enabled')
        return self

class ArchiverBlacklist(BaseModel):
    sha1: list[str] = Field(default_factory=list)
    qr: list[str] = Field(default_factory=list)


class Archiver(BaseModel):
    cmd: list[str] = Field(default_factory=lambda: [
        'zip',
        '-0',
        '--junk-paths',
        '--test',
        '-r',
        '$dst',
        '$src',
    ])
    extension: str = '.cbz'
    poll_interval_hours: int = 24
    blacklist: ArchiverBlacklist = Field(default_factory=ArchiverBlacklist)

    @model_validator(mode='after')
    def validate_command(self):
        if not self.cmd or '$dst' not in self.cmd or '$src' not in self.cmd:
            raise ValueError('archiver.cmd must contain $dst and $src')
        if (not self.extension.startswith('.') or self.extension in {'.', '..'}
                or '/' in self.extension or '\\' in self.extension):
            raise ValueError('archiver.extension must be a file extension such as .cbz')
        return self


class Config(BaseModel):
    cache: Path = APPDATA / 'cache'
    dest: Path = APPDATA / 'dest'
    pipeline_interval_seconds: int = Field(default=300, ge=0)
    db: DB
    filter: Filter
    aria2: Aria2
    qbit: QBit
    eh: EH = Field(default_factory=EH)
    log: Log = Field(default_factory=Log)
    archiver: Archiver = Field(default_factory=Archiver)

    @model_validator(mode='after')
    def separate_download_directories(self):
        if self.qbit.enabled and self.aria2.url:
            qbit_dir = self.qbit.local_dir.resolve()
            aria2_dir = self.aria2.local_dir.resolve()
            if qbit_dir == aria2_dir or qbit_dir in aria2_dir.parents or aria2_dir in qbit_dir.parents:
                raise ValueError('qbit and aria2 local directories must not overlap')
        return self


def load_config() -> Config:
    config_path = APPDATA / 'config.json'
    return Config.model_validate_json(config_path.read_text(encoding='utf-8'))


config = load_config()
