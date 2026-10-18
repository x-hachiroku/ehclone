from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ehclone.config import config
from ehclone.db.crud import dedupe as dedupe_crud, downloads, gallery, thumb
from ehclone.db.entities import Base, Category, DownloadState, Gallery, Tag, Thumb


@pytest.fixture
def session(monkeypatch):
    # Embedded, disposable storage; no PostgreSQL service is needed.
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        @contextmanager
        def sessions():
            yield session

        for module in (downloads, dedupe_crud, thumb):
            monkeypatch.setattr(module, 'session_generator', sessions)
        yield session
    engine.dispose()


def test_existing_gids_includes_placeholders_and_ignores_absent_ids(session, monkeypatch):
    @contextmanager
    def sessions():
        yield session

    monkeypatch.setattr(gallery, 'session_generator', sessions)
    session.add_all([
        Gallery(gid=1, token='a', title='Stored gallery'),
        Gallery(gid=2, token='b'),
        Gallery(gid=3, token='c', title='Outside the requested page'),
    ])
    session.commit()

    assert gallery.get_existing_gids([1, 2, 4]) == {1, 2}
    assert gallery.get_existing_gids([]) == set()


@pytest.mark.parametrize('rating,stored', [('4.5', 450), ('4.01', 401), ('4.99', 499), ('5', 500)])
def test_metadata_refresh_updates_utc_timestamp_but_state_changes_do_not(session, rating, stored):
    data = {
        'gid': 1, 'token': 'a', 'title': 'First', 'posted': '1767225600',
        'category': 'Manga', 'filecount': '10', 'filesize': '100', 'rating': '4',
        'torrents': [{'hash': 'abc', 'added': '1767225600', 'tsize': '10', 'fsize': '100'}],
    }
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    gallery.insert_gallery(session, data, {}, {})
    session.commit()
    item = session.get(Gallery, 1)
    assert before <= item.updated_at <= datetime.now(timezone.utc).replace(tzinfo=None)
    assert item.posted_at == item.torrents[0].added_at == datetime(2026, 1, 1)
    original = item.updated_at
    item.bittorrent = DownloadState.DOWNLOADING
    item.archiver_task_id = 'task'
    item.dupe_with = 2
    session.commit()
    assert item.updated_at == original

    gallery.insert_gallery(session, {**data, 'title': 'Refreshed', 'rating': rating}, {}, {})
    session.commit()
    assert item.title == 'Refreshed'
    assert item.rating == stored
    assert item.updated_at > original
    assert item.bittorrent == DownloadState.DOWNLOADING
    assert item.archiver_task_id == 'task'
    assert item.dupe_with == 2


@pytest.mark.parametrize('minimum_minutes', [0, 2, 60])
def test_metadata_refresh_runs_once_after_filtering_age(session, monkeypatch, minimum_minutes):
    monkeypatch.setattr(config.filter, 'not_before_min', minimum_minutes)
    now = datetime(2026, 1, 1, 12)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.replace(tzinfo=tz)

    monkeypatch.setattr(gallery, 'datetime', FrozenDatetime)
    @contextmanager
    def sessions():
        yield session

    monkeypatch.setattr(gallery, 'session_generator', sessions)
    cutoff = now - timedelta(minutes=minimum_minutes)
    fields = dict(token='a', title='Title', posted='0', category='Manga',
                  filecount='10', filesize='100', rating='4')
    # Include either side of the boundary and a gallery exactly at the age.
    for gid, age_delta in [(1, 1), (2, 0), (3, -1)]:
        posted_at = cutoff + timedelta(seconds=age_delta)
        data = fields | {'gid': gid, 'posted': str(int(posted_at.replace(tzinfo=timezone.utc).timestamp()))}
        gallery.insert_gallery(session, data, {}, {})
    session.commit()
    assert session.get(Gallery, 1).updated_at is None
    assert session.get(Gallery, 2).updated_at == now
    assert session.get(Gallery, 3).updated_at == now
    assert gallery.get_galleries_for_metadata_refresh(limit=25) == []

    # Advance to the exact eligibility boundary. Placeholder rows stay excluded.
    now += timedelta(seconds=1)
    session.add(Gallery(gid=4, token='a', posted_at=cutoff))
    session.commit()
    assert gallery.get_galleries_for_metadata_refresh(limit=1) == [[1, 'a']]
    posted_at = session.get(Gallery, 1).posted_at
    gallery.insert_gallery(session, fields | {
        'gid': 1, 'posted': str(int(posted_at.replace(tzinfo=timezone.utc).timestamp())),
    }, {}, {})
    session.commit()
    assert session.get(Gallery, 1).updated_at == now
    now += timedelta(days=365)
    assert gallery.get_galleries_for_metadata_refresh(limit=25) == []


def test_metadata_refresh_preserves_missing_torrents_and_updates_present_ones(session):
    data = dict(gid=1, token='a', title='Title', posted='1767225600',
                category='Manga', filecount='10', filesize='100', rating='4')
    torrent = dict(hash='old', added='1767225600', name='Original', tsize='10', fsize='100')
    gallery.insert_gallery(session, data | {'torrents': [torrent]}, {}, {})
    session.commit()
    item = session.get(Gallery, 1)
    assert [t.infohash for t in item.torrents] == ['old']

    gallery.insert_gallery(session, data | {'torrents': [torrent | {'hash': 'new'}]}, {}, {})
    session.commit()
    assert {t.infohash for t in item.torrents} == {'old', 'new'}
    gallery.insert_gallery(session, data | {'torrents': []}, {}, {})
    session.commit()
    assert {t.infohash for t in item.torrents} == {'old', 'new'}
    gallery.insert_gallery(session, data | {'torrents': [torrent | {'name': 'Updated'}]}, {}, {})
    session.commit()
    assert {t.infohash: t.name for t in item.torrents} == {'old': 'Updated', 'new': 'Original'}


def test_rating_gates_queues_and_shared_thumbnail_vectors(session, monkeypatch):
    monkeypatch.setattr(config.filter, 'min_rating', 4.01)
    for gid, rating, url in [(1, 400, 'low'), (2, None, 'missing'),
                             (3, 400, 'shared'), (4, 401, 'shared'), (5, 450, 'high')]:
        if session.get(Thumb, url) is None:
            session.add(Thumb(url=url))
        session.add(Gallery(gid=gid, token='a', title='Title', rating=rating, thumb_url=url,
                            updated_at=datetime(2020, 1, 1)))
        session.flush()

    @contextmanager
    def sessions():
        yield session

    monkeypatch.setattr(thumb, 'session_generator', sessions)
    assert list(downloads.iter_queued_gallery_ids()) == [4, 5]
    assert list(thumb.iter_unvectorized_thumb_batches()) == [['shared', 'high']]
    monkeypatch.setattr(thumb, 'BATCH_SIZE', 1)
    assert list(thumb.iter_unvectorized_thumb_batches()) == [['shared'], ['high']]
    # Existing tasks still need status checks even if their rating falls.
    session.get(Gallery, 1).bittorrent = DownloadState.DOWNLOADING
    session.flush()
    assert downloads.active_gallery_ids(session) == [1]


def test_download_queue_respects_expunged_config_without_changing_state(session, monkeypatch):
    for gid, expunged in enumerate([False, True, None], 1):
        session.add(Gallery(gid=gid, token='a', title='Title', rating=400,
                            expunged=expunged))
    session.commit()

    monkeypatch.setattr(config.eh, 'include_expunged', False)
    assert list(downloads.iter_queued_gallery_ids()) == [1, 3]
    assert not session.dirty and not session.new and not session.deleted
    assert all(g.bittorrent == g.archiver == DownloadState.QUEUED
               for g in session.query(Gallery))

    monkeypatch.setattr(config.eh, 'include_expunged', True)
    assert list(downloads.iter_queued_gallery_ids()) == [1, 2, 3]

    # Already submitted tasks still need status checks when the setting changes.
    session.get(Gallery, 2).bittorrent = DownloadState.DOWNLOADING
    session.commit()
    monkeypatch.setattr(config.eh, 'include_expunged', False)
    assert downloads.active_gallery_ids(session) == [2]


def test_download_categories_filter_queue_without_changing_state(session, monkeypatch):
    for gid, category in enumerate([Category.MANGA, Category.DOUJINSHI, Category.MISC, None], 1):
        session.add(Gallery(gid=gid, token='a', title='Title', rating=400, category=category))
    session.commit()
    monkeypatch.setattr(config.filter, 'include_categories',
                        [Category.MANGA, Category.DOUJINSHI, Category.MANGA])
    monkeypatch.setattr(config.eh, 'categories', [Category.MISC])
    monkeypatch.setattr(downloads, 'BATCH_SIZE', 1)

    assert list(downloads.iter_queued_gallery_ids()) == [1, 2]
    assert not session.dirty and not session.new and not session.deleted
    assert all(g.bittorrent == g.archiver == DownloadState.QUEUED
               and g.dupe_with is None for g in session.query(Gallery))

    monkeypatch.setattr(config.filter, 'include_categories', [])
    assert list(downloads.iter_queued_gallery_ids()) == [1, 2, 3, 4]

    session.get(Gallery, 3).bittorrent = DownloadState.DOWNLOADING
    session.get(Gallery, 4).archiver = DownloadState.DOWNLOADING
    session.commit()
    monkeypatch.setattr(config.filter, 'include_categories', [Category.MANGA])
    assert downloads.active_gallery_ids(session) == [4, 3]


def test_base_candidates_use_mature_metadata_vectors_rating_and_current_winners(session, monkeypatch):
    monkeypatch.setattr(config.filter, 'min_rating', 4.01)
    monkeypatch.setattr(config.filter, 'not_before_min', 2)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add_all([Thumb(url='vector', mobile_net_v3=[1.0] * 576), Thumb(url='empty')])
    fields = dict(token='a', title='Title', rating=401, thumb_url='vector', filecount=10,
                  posted_at=now - timedelta(days=1), updated_at=now - timedelta(minutes=3))
    variations = [
        {}, {'updated_at': now - timedelta(seconds=90)}, {'posted_at': None},
        {'rating': 400}, {'rating': None}, {'thumb_url': 'empty'},
        {'thumb_url': None}, {'dupe_with': 1}, {'title': None},
        {'updated_at': None}, {'posted_at': now},
    ]
    for gid, extra in enumerate(variations, 1):
        session.add(Gallery(gid=gid, **(fields | extra)))
    session.flush()
    monkeypatch.setattr(dedupe_crud, 'BATCH_SIZE', 1)
    assert list(dedupe_crud.iter_base_candidates()) == [(1, 'a'), (2, 'a'), (3, 'a'), (11, 'a')]


def test_include_tags_requires_all_tags_without_changing_stored_state(session, monkeypatch):
    english = Tag(namespace='language', name='english')
    uncensored = Tag(namespace='other', name='uncensored')
    wrong_namespace = Tag(namespace='temp', name='english')
    for gid, tags in enumerate([
        [english, uncensored], [english], [uncensored], [],
        [wrong_namespace, uncensored],
    ], 1):
        session.add(Gallery(gid=gid, token='a', title='Title', rating=400, tags=tags,
                            updated_at=datetime(2020, 1, 1)))
    session.commit()
    monkeypatch.setattr(config.filter, 'include_tags', ['language:english', 'other:uncensored'])
    assert list(downloads.iter_queued_gallery_ids()) == [1]
    assert not session.dirty and not session.new and not session.deleted
    assert all(g.bittorrent == g.archiver == DownloadState.QUEUED
               and g.dupe_with is None for g in session.query(Gallery))

    monkeypatch.setattr(config.filter, 'include_tags', ['english', 'other:uncensored'])
    assert list(downloads.iter_queued_gallery_ids()) == [5]
    monkeypatch.setattr(config.filter, 'include_tags', [])
    assert list(downloads.iter_queued_gallery_ids()) == [1, 2, 3, 4, 5]

    session.get(Gallery, 4).bittorrent = DownloadState.DOWNLOADING
    session.commit()
    monkeypatch.setattr(config.filter, 'include_tags', ['language:english', 'other:uncensored'])
    assert downloads.active_gallery_ids(session) == [4]


def test_recursive_redirection_preserves_metadata_timestamps(session):
    timestamp = datetime(2026, 1, 1)
    for gid, target in [(1, None), (2, None), (3, 1), (4, 3), (5, 2), (6, None)]:
        session.add(Gallery(gid=gid, token='a', dupe_with=target, updated_at=timestamp))
    session.flush()
    descendants = dedupe_crud.expand_dupe_links(session, {1, 2})
    assert {g.gid for g in descendants} == {3, 4, 5}
    roots = [session.get(Gallery, gid) for gid in (1, 2)]
    dedupe_crud.persist_dedupe_resolution(session, 2, roots + descendants, checked_gid=1)
    session.commit()
    assert {g.gid: g.dupe_with for g in session.query(Gallery)} == {
        1: 2, 2: None, 3: 2, 4: 2, 5: 2, 6: None,
    }
    assert all(g.updated_at == timestamp for g in session.query(Gallery))


def test_thumbnail_paging_filters_tags_and_skips_vectors_completed_on_prior_pages(session, monkeypatch):
    from unittest.mock import Mock
    from ehclone.core import sync_thumbs

    monkeypatch.setattr(config.filter, 'min_rating', 4.01)
    monkeypatch.setattr(config.filter, 'include_tags', ['language:english', 'other:uncensored'])
    monkeypatch.setattr(thumb, 'BATCH_SIZE', 2)
    english = Tag.from_str('language:english')
    uncensored = Tag.from_str('other:uncensored')
    for gid, url, rating, tags in [
        (1, 'shared', 401, [english, uncensored]),
        (2, 'broken', 401, [english, uncensored]),
        (3, 'low', 400, [english, uncensored]),
        (4, 'shared', 401, [english, uncensored]),
        (5, 'excluded', 401, [english]),
        (6, 'last', 401, [english, uncensored]),
    ]:
        image = session.get(Thumb, url)
        if image is None:
            image = Thumb(url=url)
        session.add(Gallery(gid=gid, token='a', thumb=image, rating=rating, tags=tags))
        session.flush()

    @contextmanager
    def sessions():
        yield session

    monkeypatch.setattr(thumb, 'session_generator', sessions)
    download = Mock(side_effect=lambda url: url.encode())
    monkeypatch.setattr(sync_thumbs, 'download_thumb', download)

    def encode(url, data):
        if url == 'broken':
            return False
        thumb.update_thumb_vector(url, [1.0] * 576)
        return True

    monkeypatch.setattr(sync_thumbs, 'update_encode', encode)
    sync_thumbs.run()
    assert [call.args[0] for call in download.call_args_list] == ['shared', 'broken', 'last']
    download.reset_mock()
    sync_thumbs.run()
    assert [call.args[0] for call in download.call_args_list] == ['broken']


def test_download_paging_survives_state_changes_failures_and_stops_when_full(session, monkeypatch):
    from unittest.mock import Mock
    from ehclone.core import download_manager

    monkeypatch.setattr(downloads, 'BATCH_SIZE', 2)
    monkeypatch.setattr(config.qbit, 'enabled', False)
    monkeypatch.setattr(config.aria2, 'task_limit', 3)
    for gid in range(1, 11):
        session.add(Gallery(
            gid=gid, token='a', title='Title', rating=400, updated_at=datetime(2020, 1, 1),
            archiver=DownloadState.DOWNLOADING if gid >= 8 else DownloadState.QUEUED,
            archiver_task_id=str(gid) if gid >= 8 else None,
        ))
    session.commit()

    @contextmanager
    def sessions():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    monkeypatch.setattr(download_manager, 'session_generator', sessions)
    monkeypatch.setattr(download_manager, 'Aria2Client', Mock())
    manager = download_manager.DownloadManager()
    checked = []
    enqueued = []

    def check(item):
        checked.append(item.gid)
        if item.gid == 9:
            raise ConnectionError('status unavailable')
        item.archiver = DownloadState.COMPLETED

    def cleanup(item):
        item.archiver_task_id = None

    enqueue = manager._enqueue

    def enqueue_with_failure(item, qbit_active, aria2_active):
        enqueued.append(item.gid)
        if item.gid == 1:
            raise ConnectionError('submission unavailable')
        enqueue(item, qbit_active, aria2_active)

    def start(item):
        item.archiver = DownloadState.DOWNLOADING

    monkeypatch.setattr(manager, '_check', check)
    monkeypatch.setattr(manager, '_cleanup_archive', cleanup)
    monkeypatch.setattr(manager, '_enqueue', enqueue_with_failure)
    monkeypatch.setattr(manager, '_start_archive', start)
    manager.run()
    assert checked == [10, 9, 8]
    assert enqueued == [1, 2, 3]
    assert downloads.active_counts(session) == (0, 3)
    assert list(downloads.iter_queued_gallery_ids()) == [1, 4, 5, 6, 7]


@pytest.mark.parametrize('kind', ['dedupe', 'queue', 'thumb'])
@pytest.mark.parametrize('stop_early', [False, True])
def test_iterators_close_read_sessions_and_page_through_changing_rows(
        session, monkeypatch, kind, stop_early):
    module = {'dedupe': dedupe_crud, 'queue': downloads, 'thumb': thumb}[kind]
    monkeypatch.setattr(module, 'BATCH_SIZE', 2)
    for gid in range(1, 6):
        image = Thumb(url=str(gid), mobile_net_v3=None if kind == 'thumb' else [1.0] * 576)
        session.add(Gallery(gid=gid, token='a', title='Title', rating=400, thumb=image, filecount=10,
                            updated_at=datetime(2020, 1, 1)))
    session.flush()
    opened = 0
    closed = 0

    @contextmanager
    def sessions():
        nonlocal opened, closed
        opened += 1
        try:
            yield session
        finally:
            closed += 1

    monkeypatch.setattr(module, 'session_generator', sessions)
    rows = {
        'dedupe': dedupe_crud.iter_base_candidates,
        'queue': downloads.iter_queued_gallery_ids,
        'thumb': thumb.iter_unvectorized_thumb_batches,
    }[kind]()
    assert opened == 0
    seen = []
    for row in rows:
        assert opened == closed
        gids = [row[0]] if kind == 'dedupe' else [row] if kind == 'queue' else list(map(int, row))
        seen.extend(gids)
        for gid in gids:
            item = session.get(Gallery, gid)
            if kind == 'dedupe':
                item.dupe_with = 99
            elif kind == 'queue':
                item.archiver = DownloadState.DOWNLOADING
            else:
                item.thumb.mobile_net_v3 = [1.0] * 576
        if stop_early:
            break
    rows.close()
    assert opened == closed
    if not stop_early:
        assert seen == [1, 2, 3, 4, 5]


@pytest.mark.parametrize('enabled', [False, True])
def test_download_queue_requires_dedupe_metadata_when_enabled(session, monkeypatch, enabled):
    monkeypatch.setattr(config.filter.dedupe, 'enabled', enabled)
    monkeypatch.setattr(config.filter, 'not_before_min', 60)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    fields = dict(token='a', title='Title', rating=400, posted_at=now - timedelta(days=1),
                  updated_at=now - timedelta(minutes=120))
    variations = [
        {},
        {'deduped_at': now - timedelta(minutes=1)},
        {'posted_at': now, 'deduped_at': now},
        {'posted_at': None, 'deduped_at': now},
        {'deduped_at': now, 'updated_at': None},
        {'deduped_at': now, 'dupe_with': 2},
        {'deduped_at': now, 'updated_at': now - timedelta(minutes=2)},
    ]
    for gid, extra in enumerate(variations, 1):
        session.add(Gallery(gid=gid, **(fields | extra)))
    session.flush()
    assert list(downloads.iter_queued_gallery_ids()) == ([2, 3, 4, 7] if enabled else [1, 2, 3, 4, 5, 7])


@pytest.fixture
def dedupe_session(session, monkeypatch):
    import json
    import numpy as np
    from pgvector.sqlalchemy import Vector
    from sqlalchemy import func
    from ehclone.core import dedupe

    # SQLite has no pgvector operator; use the same cosine distance calculation
    # while exercising the real candidate queries and transactional workflow.
    def cosine_distance(left, right):
        left, right = np.array(json.loads(left)), np.array(json.loads(right))
        return float(1 - np.dot(left, right) / (np.linalg.norm(left) * np.linalg.norm(right)))

    session.connection().connection.create_function('cosine_distance', 2, cosine_distance)
    monkeypatch.setattr(Vector.comparator_factory, 'cosine_distance',
                        lambda self, other: func.cosine_distance(self.expr, json.dumps(other)))

    @contextmanager
    def sessions():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    monkeypatch.setattr(dedupe, 'session_generator', sessions)
    monkeypatch.setattr(config.filter.dedupe, 'enabled', True)
    return session


def test_dupe_pool_returns_nearest_eligible_galleries_with_tags(dedupe_session, monkeypatch):
    import math

    session = dedupe_session
    monkeypatch.setattr(config.filter.dedupe, 'candidate_limit', 2)
    monkeypatch.setattr(config.filter.dedupe, 'distance_threshold', 0.05)
    for gid, angle, pages in [(1, 0, 10), (2, 0.01, 100), (3, 0.3, 10),
                              (4, 0.2, 10), (5, 0.1, 10), (6, 1.0, 10)]:
        session.add(Gallery(
            gid=gid, token='a', title='Title', rating=400, filecount=pages,
            updated_at=datetime(2020, 1, 1),
            thumb=Thumb(url=str(gid), mobile_net_v3=[math.cos(angle), math.sin(angle)] + [0.] * 574),
        ))
    session.get(Gallery, 5).tags = [Tag(namespace='artist', name='a'), Tag(namespace='group', name='b')]
    session.commit()
    base = session.get(Gallery, 1)
    pool = dedupe_crud.fetch_dupe_pool(session, base, [1.] + [0.] * 575)
    assert [g.gid for g in pool] == [5, 4]
    assert len(pool[0].tags) == 2

    monkeypatch.setattr(config.filter.dedupe, 'candidate_limit', 10)
    assert [g.gid for g in dedupe_crud.fetch_dupe_pool(session, base, [1.] + [0.] * 575)] == [5, 4, 3]


def test_dupe_pool_excludes_page_and_distance_boundaries(dedupe_session, monkeypatch):
    session = dedupe_session
    monkeypatch.setattr(config.filter.dedupe, 'page_tolerance', 8)
    monkeypatch.setattr(config.filter.dedupe, 'distance_threshold', 1)
    vector = [1.] + [0.] * 575
    for gid, pages, embedding in [
        (1, 10, vector), (2, 2, vector), (3, 3, vector),
        (4, 17, vector), (5, 18, vector), (6, None, vector),
        (7, 10, [0., 1.] + [0.] * 574),
        (8, 10, [-1.] + [0.] * 575),
    ]:
        session.add(Gallery(
            gid=gid, token='a', title='Title', rating=400, filecount=pages,
            updated_at=datetime(2020, 1, 1),
            thumb=Thumb(url=str(gid), mobile_net_v3=embedding),
        ))
    session.commit()
    assert {g.gid for g in dedupe_crud.fetch_dupe_pool(session, session.get(Gallery, 1), vector)} == {3, 4}
    assert dedupe_crud.fetch_dupe_pool(session, session.get(Gallery, 6), vector) == []


@pytest.mark.parametrize('pages,minimum,rate,ad,normal_tolerance', [
    (40, 4, 0.025, 8, 4),
    (160, 4, 0.025, 8, 4),
    (161, 4, 0.025, 8, 5),
    (400, 4, 0.025, 8, 10),
    (400, 6, 0, 3, 6),
    (400, 4, 0.025, 0, 10),
    (40, 0, 0, 0, 0),
])
@pytest.mark.parametrize('tagged', ['neither', 'base', 'candidate', 'both', 'wrong_namespace'])
def test_dupe_pool_uses_scaled_tolerance_and_conditional_ad_allowance(
    dedupe_session, monkeypatch, pages, minimum, rate, ad, normal_tolerance, tagged,
):
    session = dedupe_session
    monkeypatch.setattr(config.filter.dedupe, 'page_tolerance', minimum)
    monkeypatch.setattr(config.filter.dedupe, 'page_tolerance_rate', rate)
    monkeypatch.setattr(config.filter.dedupe, 'page_tolerance_ad', ad)
    tolerance = normal_tolerance + (ad if tagged in {'base', 'candidate', 'both'} else 0)
    vector = [1.] + [0.] * 575
    tag = Tag(namespace='artist' if tagged == 'wrong_namespace' else 'other', name='extraneous ads')
    session.add(Thumb(url='shared', mobile_net_v3=vector))
    base = Gallery(
        gid=1, token='a', title='Base', rating=400, filecount=pages,
        updated_at=datetime(2020, 1, 1), thumb_url='shared',
        tags=[tag] if tagged in {'base', 'both', 'wrong_namespace'} else [],
    )
    session.add(base)
    expected = set()
    for gid, difference in enumerate(sorted({
        -tolerance - 1, -tolerance, -tolerance + 1, 0,
        tolerance - 1, tolerance, tolerance + 1,
    }), 2):
        session.add(Gallery(
            gid=gid, token='a', title='Candidate', rating=400, filecount=pages + difference,
            updated_at=datetime(2020, 1, 1), thumb_url='shared',
            tags=[tag] if tagged in {'candidate', 'both', 'wrong_namespace'} else [],
        ))
        if abs(difference) < tolerance:
            expected.add(gid)
    session.commit()
    assert {g.gid for g in dedupe_crud.fetch_dupe_pool(session, base, vector)} == expected


def test_dupe_pool_checks_ad_tags_after_candidate_limit(dedupe_session, monkeypatch):
    import math

    session = dedupe_session
    monkeypatch.setattr(config.filter.dedupe, 'page_tolerance', 4)
    monkeypatch.setattr(config.filter.dedupe, 'page_tolerance_rate', 0.025)
    monkeypatch.setattr(config.filter.dedupe, 'page_tolerance_ad', 8)
    monkeypatch.setattr(config.filter.dedupe, 'candidate_limit', 2)
    for gid, angle, pages in [
        (1, 0, 40), (2, 0.1, 48), (3, 0.2, 48), (4, 0.3, 42), (5, 0.01, 52),
    ]:
        session.add(Gallery(
            gid=gid, token='a', title='Title', rating=400, filecount=pages,
            updated_at=datetime(2020, 1, 1),
            thumb=Thumb(url=str(gid), mobile_net_v3=[math.cos(angle), math.sin(angle)] + [0.] * 574),
            tags=[Tag(namespace='other', name='extraneous ads')] if gid == 3 else [],
        ))
    session.commit()
    base = session.get(Gallery, 1)
    vector = [1.] + [0.] * 575
    # The expanded query excludes 5 at its boundary. Candidate 2 consumes a slot,
    # then gets rejected for lacking the ad tag; candidate 3 keeps its allowance.
    assert [g.gid for g in dedupe_crud.fetch_dupe_pool(session, base, vector)] == [3]
    monkeypatch.setattr(config.filter.dedupe, 'candidate_limit', 3)
    assert [g.gid for g in dedupe_crud.fetch_dupe_pool(session, base, vector)] == [3, 4]


def test_pending_winner_checks_its_own_neighbors_and_later_matches_new_gallery(dedupe_session):
    import math
    from ehclone.core import dedupe

    session = dedupe_session
    for gid, angle in enumerate([0, 0.25, 0.5], 1):
        session.add(Gallery(
            gid=gid, token='a', title='Title', rating=350 + gid * 50, filecount=10,
            posted_at=datetime(2020, 1, 1), updated_at=datetime(2020, 1, 2),
            thumb=Thumb(url=str(gid), mobile_net_v3=[math.cos(angle), math.sin(angle)] + [0.] * 574),
        ))
    session.commit()
    dedupe.run()
    assert [session.get(Gallery, gid).dupe_with for gid in range(1, 4)] == [3, 3, None]
    checked_at = session.get(Gallery, 3).deduped_at
    assert checked_at is not None and checked_at.tzinfo is None
    assert list(dedupe_crud.iter_base_candidates()) == []
    assert list(downloads.iter_queued_gallery_ids()) == [3]

    session.add(Gallery(gid=4, token='a', title='New duplicate', rating=400, filecount=10,
                        updated_at=datetime(2020, 1, 1), thumb_url='3'))
    session.commit()
    dedupe.run()
    dedupe.run()
    assert session.get(Gallery, 4).dupe_with == 3
    assert session.get(Gallery, 4).deduped_at is not None
    assert session.get(Gallery, 3).deduped_at == checked_at


@pytest.mark.parametrize('vector,checked', [(None, False), ([0.] * 576, False), ([1.] * 576, True)])
def test_dedupe_marks_successful_singletons_only(dedupe_session, vector, checked):
    from ehclone.core import dedupe

    session = dedupe_session
    item = Gallery(gid=1, token='a', title='Title', rating=400, filecount=10,
                   updated_at=datetime(2020, 1, 1), thumb=Thumb(url='image', mobile_net_v3=vector))
    session.add(item)
    session.commit()
    dedupe.run()
    assert (item.deduped_at is not None) == checked
    assert item.dupe_with is None
    assert list(downloads.iter_queued_gallery_ids()) == ([1] if checked else [])


def test_dedupe_error_rolls_back_marker_and_duplicate_links(dedupe_session, monkeypatch):
    from ehclone.core import dedupe

    session = dedupe_session
    session.add(Thumb(url='image', mobile_net_v3=[1.] * 576))
    for gid in (1, 2):
        session.add(Gallery(gid=gid, token='a', title='Title', rating=400, filecount=10,
                            updated_at=datetime(2020, 1, 1), thumb_url='image'))
    session.commit()
    persist = dedupe.persist_dedupe_resolution

    def fail(*args):
        persist(*args)
        raise RuntimeError('transaction failed')

    monkeypatch.setattr(dedupe, 'persist_dedupe_resolution', fail)
    with pytest.raises(RuntimeError, match='transaction failed'):
        dedupe._process_gallery(1)
    assert all(g.deduped_at is None and g.dupe_with is None for g in session.query(Gallery))


def test_refreshed_metadata_requires_a_new_check(dedupe_session, monkeypatch):
    from ehclone.core import dedupe

    session = dedupe_session
    monkeypatch.setattr(config.filter, 'not_before_min', 0)
    data = dict(gid=1, token='a', title='Title', rating='4', posted='1767225600',
                category='Manga', filecount='10', filesize='100', thumb='image')
    gallery.insert_gallery(session, data, {}, {})
    thumb.update_thumb_vector('image', [1.] * 576)
    session.commit()
    dedupe.run()
    item = session.get(Gallery, 1)
    checked_at = item.deduped_at
    assert checked_at is not None
    assert list(dedupe_crud.iter_base_candidates()) == []

    gallery.insert_gallery(session, {**data, 'rating': '4.5'}, {}, {})
    session.commit()
    assert item.updated_at > checked_at
    assert list(dedupe_crud.iter_base_candidates()) == [(1, 'a')]
    dedupe.run()
    assert item.deduped_at >= item.updated_at
    assert list(dedupe_crud.iter_base_candidates()) == []
    assert list(downloads.iter_queued_gallery_ids()) == [1]


def test_excluded_duplicate_winner_still_suppresses_eligible_download(dedupe_session, monkeypatch):
    from ehclone.core import dedupe

    session = dedupe_session
    monkeypatch.setattr(config.filter, 'include_tags', ['other:uncensored'])
    session.add(Thumb(url='shared', mobile_net_v3=[1.] * 576))
    for gid, rating, tags in [(1, 400, [Tag.from_str('other:uncensored')]), (2, 500, [])]:
        session.add(Gallery(gid=gid, token='a', title='Title', rating=rating, filecount=10,
                            updated_at=datetime(2020, 1, 1), thumb_url='shared', tags=tags))
    session.commit()
    dedupe.run()
    assert session.get(Gallery, 1).dupe_with == 2
    assert session.get(Gallery, 2).deduped_at is not None
    assert list(downloads.iter_queued_gallery_ids()) == []


def test_download_completion_survives_source_cleanup_rollback(session, tmp_path, monkeypatch):
    from pathlib import Path
    from unittest.mock import Mock
    from ehclone.core import download_manager

    item = Gallery(gid=1, token='a', archiver=DownloadState.DOWNLOADING, archiver_task_id='abc')
    session.add(item)
    session.commit()

    @contextmanager
    def sessions():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    monkeypatch.setattr(download_manager, 'session_generator', sessions)
    monkeypatch.setattr(config.qbit, 'enabled', False)
    monkeypatch.setattr(config.aria2, 'local_dir', tmp_path)
    aria2 = Mock()
    aria2.get_status.return_value = {'status': 'complete'}
    monkeypatch.setattr(download_manager, 'Aria2Client', Mock(return_value=aria2))
    validate = Mock(return_value=True)
    monkeypatch.setattr(download_manager, 'validate', validate)
    manager = download_manager.DownloadManager()
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', Mock(side_effect=PermissionError('busy')))
        manager.run()
    session.expire_all()
    assert item.archiver == DownloadState.COMPLETED
    assert item.archiver_task_id == 'abc'
    assert downloads.active_counts(session) == (0, 0)
    manager.run()
    assert item.archiver_task_id is None
    validate.assert_called_once()
