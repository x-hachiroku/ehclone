from unittest.mock import Mock

import numpy as np
import pytest

from ehclone.config import config
from ehclone.core import dedupe as filtering
from ehclone.core.dedupe import _ensure_vector, rate_gallery
from ehclone.db.entities import Gallery, Tag, Thumb


def test_ranking_is_stable_and_preserves_original_rating():
    item = Gallery(gid=1, rating=400, filecount=10, expunged=False)
    item.tags = [Tag(namespace='other', name='uncensored')]
    first = rate_gallery(item)
    assert rate_gallery(item) == first
    assert item.rating == 400


@pytest.mark.parametrize('stored,score', [(None, 0), (0, 0), (401, 80.2), (499, 99.8), (500, 100)])
def test_rating_score_converts_hundredths_to_stars(monkeypatch, stored, score):
    monkeypatch.setattr(config.filter.dedupe, 'rating_factor', 20)
    item = Gallery(gid=1, rating=stored)
    assert rate_gallery(item) == pytest.approx(score)
    assert item.rating == stored


def test_cached_numpy_vector_needs_no_network_or_model():
    item = Gallery(gid=1)
    item.thumb = Thumb(url='image', mobile_net_v3=np.array([3.0, 4.0]))
    assert _ensure_vector(item) == [0.6, 0.8]


def test_missing_vector_does_not_download_thumbnail(monkeypatch):
    from unittest.mock import Mock
    from ehclone.core.session import ehs

    item = Gallery(gid=1, thumb_url='https://thumbs.example/image.jpg')
    item.thumb = Thumb(url=item.thumb_url)
    get = Mock()
    monkeypatch.setattr(ehs, 'get', get)
    assert _ensure_vector(item) is None
    get.assert_not_called()


@pytest.mark.parametrize('left,right,expected', [
    ({}, {}, True),
    ({'language': ['english']}, {'language': ['english']}, True),
    ({'language': ['english', 'translated']},
     {'language': ['translated', 'english']}, True),
    ({'language': ['english']}, {}, False),
    ({'language': ['english']}, {'language': ['japanese']}, False),
    ({'group': ['g']}, {}, False),
    ({'group': ['g']}, {'group': ['g']}, True),
    ({'group': ['g']}, {'group': ['h']}, False),
    ({'group': ['g']}, {'group': ['g', 'h']}, False),
    ({'group': ['g', 'h']}, {'group': ['h', 'g']}, False),
    ({'group': ['g']}, {'artist': ['a'], 'group': ['g']}, True),
    ({'artist': ['a']}, {}, False),
    ({'artist': ['a']}, {'artist': ['b']}, False),
    ({'artist': ['a']}, {'artist': ['a']}, True),
    ({'artist': ['a', 'b']}, {'artist': ['b', 'a']}, True),
    ({'artist': ['a', 'b']}, {'artist': ['a']}, False),
    ({'artist': ['a'], 'group': ['g']}, {'artist': ['b'], 'group': ['g']}, True),
    ({'artist': ['a'], 'group': ['g', 'h']},
     {'artist': ['b'], 'group': ['g', 'h']}, False),
    ({'artist': ['a'], 'group': ['g']}, {'artist': ['a'], 'group': ['h']}, True),
    ({'artist': ['a'], 'group': ['g']}, {'artist': ['b'], 'group': ['h']}, False),
    ({'artist': ['a', 'b', 'c'], 'group': ['g']},
     {'artist': ['c', 'a', 'b'], 'group': ['h']}, True),
    ({'artist': ['a'], 'group': ['g', 'h']},
     {'artist': ['a'], 'group': ['i', 'j']}, True),
    ({'artist': ['a', 'b', 'c', 'd'], 'group': ['g', 'h', 'i', 'j']},
     {'artist': ['a', 'b', 'c', 'd'], 'group': ['g', 'h', 'i', 'j']}, True),
    ({'artist': ['a', 'b', 'c', 'd'], 'group': ['g']},
     {'artist': ['e', 'f', 'g', 'h', 'i'], 'group': ['h']}, True),
    ({'artist': ['a', 'b', 'c']}, {'artist': ['a', 'b', 'c', 'd']}, False),
    ({}, {'artist': ['a', 'b', 'c', 'd']}, False),
    ({'artist': ['a'], 'language': ['english']},
     {'artist': ['a'], 'language': ['japanese']}, False),
    ({'group': ['g'], 'language': ['english']},
     {'group': ['g'], 'language': ['japanese']}, False),
    ({'group': ['g'], 'language': ['english']}, {'group': ['g']}, False),
    ({'artist': ['a', 'b', 'c', 'd'], 'language': ['english']},
     {'artist': ['a', 'b', 'c', 'd']}, False),
])
def test_dupe_metadata_matches_language_and_documented_artist_group_rules(left, right, expected):
    galleries = []
    for namespaces in (left, right):
        gallery = Gallery()
        gallery.tags = [Tag(namespace=namespace, name=name)
                        for namespace, names in namespaces.items() for name in names]
        galleries.append(gallery)
    assert filtering._matches_dupe_metadata(*galleries) is expected
    assert filtering._matches_dupe_metadata(*reversed(galleries)) is expected


def test_group_filters_pool_by_metadata(monkeypatch):
    base = Gallery(gid=1, filecount=10)
    base.tags = [Tag(namespace='artist', name='a'), Tag(namespace='group', name='g')]
    base.thumb = Thumb(url='base', mobile_net_v3=[1.0, 0.0])
    matching = Gallery(gid=2)
    matching.tags = [Tag(namespace='artist', name='a'), Tag(namespace='group', name='h')]
    same_group = Gallery(gid=3)
    same_group.tags = [Tag(namespace='artist', name='b'), Tag(namespace='group', name='g')]
    other_language = Gallery(gid=4)
    other_language.tags = [Tag(namespace='artist', name='a'),
                           Tag(namespace='group', name='g'),
                           Tag(namespace='language', name='english')]
    other_artist = Gallery(gid=5)
    other_artist.tags = [Tag(namespace='artist', name='b'), Tag(namespace='group', name='h')]
    pool = Mock(return_value=[matching, same_group, other_language, other_artist])
    monkeypatch.setattr(filtering, 'fetch_dupe_pool', pool)
    session = Mock()

    assert filtering._collect_dupe_group(session, base) == [base, matching, same_group]


def test_ranking_preserves_fractional_scores(monkeypatch):
    monkeypatch.setattr(config.filter.dedupe, 'rating_factor', 0.1)
    first = Gallery(gid=1, rating=400)
    second = Gallery(gid=2, rating=401)
    assert filtering._select_winner([first, second]) is second


def test_group_winner_uses_configured_score_and_stable_ties(monkeypatch):
    monkeypatch.setattr(config.filter.dedupe, 'tag_biases', {'other:uncensored': 50})
    first = Gallery(gid=1, rating=500, filecount=10)
    preferred = Gallery(gid=2, rating=400, filecount=10)
    tied = Gallery(gid=3, rating=400, filecount=10)
    preferred.tags = [Tag(namespace='other', name='uncensored')]
    tied.tags = [Tag(namespace='other', name='uncensored')]
    assert filtering._select_winner([first, tied, preferred]) is preferred


def test_dedupe_continues_after_failure(monkeypatch):
    monkeypatch.setattr(config.filter.dedupe, 'enabled', True)
    candidates = Mock(return_value=iter([(gid, 'a') for gid in range(1, 6)]))
    monkeypatch.setattr(filtering, 'iter_base_candidates', candidates)
    process = Mock(side_effect=[set(), ValueError('broken'), set(), set(), set()])
    monkeypatch.setattr(filtering, '_process_gallery', process)
    filtering.run()
    assert [call.args[0] for call in process.call_args_list] == [1, 2, 3, 4, 5]
