from pathlib import Path

import pytest
from pydantic import ValidationError

from ehclone.config import Aria2, Archiver, Config, EH, Filter, FilterDedupe, QBit, config
from ehclone.db.entities import Category


def test_recheck_defaults_to_disabled_and_accepts_a_starting_gid():
    assert EH().recheck == 0
    assert EH(recheck=12345).recheck == 12345


def test_empty_categories_include_every_category_and_duplicates_are_ignored():
    assert EH(categories=[]).get_f_cats() == 0
    assert EH(categories=[Category.MANGA, Category.MANGA]).get_f_cats() == 1019


def test_download_categories_default_to_unrestricted_and_accept_category_names():
    assert Filter(dedupe={}).include_categories == []
    assert Filter(dedupe={}, include_categories=['Manga', 'Doujinshi']).include_categories == [
        Category.MANGA, Category.DOUJINSHI,
    ]


def test_minimum_rating_uses_stars_and_accepts_hundredths():
    for rating in (0, 4.01, 5):
        assert Filter(dedupe={}, min_rating=rating).min_rating == rating
    for rating in (-0.01, 5.01, 80):
        with pytest.raises(ValidationError):
            Filter(dedupe={}, min_rating=rating)


def test_dedupe_page_tolerance_defaults():
    dedupe = FilterDedupe()
    assert dedupe.page_tolerance == 4
    assert dedupe.page_tolerance_rate == 0.025
    assert dedupe.page_tolerance_ad == 8


@pytest.mark.parametrize('factory,data', [
    (EH, {'max_retry': -1}),
    (Filter, {'dedupe': {}, 'include_categories': 'Manga'}),
    (Filter, {'dedupe': {}, 'include_categories': ['Unknown']}),
    (Filter, {'dedupe': {}, 'include_categories': [1]}),
    (Filter, {'dedupe': {}, 'include_tags': 'language:english'}),
    (Filter, {'dedupe': {}, 'include_tags': [1]}),
    (Filter, {'dedupe': {}, 'not_before_min': -1}),
    (Filter, {'dedupe': {'candidate_limit': 0}}),
    (Filter, {'dedupe': {'page_tolerance': -1}}),
    (Filter, {'dedupe': {'page_tolerance_rate': -0.01}}),
    (Filter, {'dedupe': {'page_tolerance_ad': -1}}),
    (QBit, {'url': 'http://qbit'}),
    (Aria2, {'url': 'http://aria2'}),
    (Archiver, {'extension': '/../escape'}),
    (Archiver, {'cmd': ['7z', '$src']}),
    (Archiver, {'cmd': ['zip', '%dst', '%src']}),
])
def test_invalid_configuration_fails_early(factory, data):
    with pytest.raises(ValidationError):
        factory(**data)


def test_disabled_clients_accept_missing_connection_settings():
    QBit(enabled=False, url=None, local_dir=None, remote_dir=None)
    Aria2(url=None, local_dir=None, remote_dir=None)


def test_example_configuration_is_valid():
    example = Path(__file__).resolve().parents[1] / 'appdata' / 'config.example.json'
    Config.model_validate_json(example.read_text(encoding='utf-8'))


def test_overlapping_client_directories_are_rejected():
    data = config.model_dump()
    data['aria2']['local_dir'] = '/downloads'
    data['qbit']['local_dir'] = '/downloads/qbit'
    with pytest.raises(ValidationError, match='must not overlap'):
        Config.model_validate(data)
