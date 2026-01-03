from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ehclone.core import download_manager
from ehclone.core.download_manager import DownloadManager
from ehclone.db.entities import Base, DownloadState as State, Gallery, Tag, Torrent


@pytest.fixture(params=['UTC0', 'UTC+7', 'UTC-8'])
def local_timezone(request, monkeypatch):
    if not hasattr(time, 'tzset'):
        pytest.skip('Host timezone changes require time.tzset')
    with monkeypatch.context() as patch:
        patch.setenv('TZ', request.param)
        time.tzset()
        try:
            yield
        finally:
            patch.undo()
            time.tzset()


def test_archive_submission_records_deterministic_id_after_success(monkeypatch):
    manager = DownloadManager()
    manager.aria2 = Mock()
    manager.aria2.get_status.return_value = None
    gallery = Gallery(gid=1, token='token', rating=400, updated_at=datetime(2020, 1, 1), archiver=State.QUEUED,
                      bittorrent=State.DOWNLOAD_FAILED)
    monkeypatch.setattr(download_manager.ehs, 'archive_url', Mock(return_value='https://archive'))

    def submit(gid, url, out):
        assert gallery.archiver == State.QUEUED
        assert gallery.archiver_task_id is None
        return 'ec00000000000001'

    manager.aria2.add_task.side_effect = submit
    manager._enqueue(gallery, 0, 0)
    manager.aria2.add_task.assert_called_once_with(1, 'https://archive', '1_token.cbz')
    manager.aria2.get_status.assert_called_once_with(1)
    assert gallery.archiver_task_id == 'ec00000000000001'
    assert gallery.archiver == State.DOWNLOADING


def test_failed_archive_submission_remains_queued_for_retry(monkeypatch):
    manager = DownloadManager()
    aria2 = Mock()
    aria2.get_status.return_value = None
    manager.aria2 = aria2
    gallery = Gallery(gid=1, token='token', archiver=State.QUEUED)
    monkeypatch.setattr(download_manager.ehs, 'archive_url', Mock(return_value='https://archive'))
    aria2.add_task.side_effect = ConnectionError('offline')
    with pytest.raises(ConnectionError):
        manager._start_archive(gallery)
    assert gallery.archiver == State.QUEUED
    assert gallery.archiver_task_id is None
    assert manager.aria2 is None

    manager.aria2 = aria2
    aria2.add_task.side_effect = None
    aria2.add_task.return_value = 'ec00000000000001'
    manager._start_archive(gallery)
    assert gallery.archiver == State.DOWNLOADING
    assert gallery.archiver_task_id == 'ec00000000000001'


@pytest.mark.parametrize('status', ['active', 'waiting', 'complete', 'error'])
def test_archive_reuses_task_after_lost_submission_response(monkeypatch, status):
    manager = DownloadManager()
    aria2 = Mock()
    manager.aria2 = aria2
    gallery = Gallery(gid=123, token='token', archiver=State.QUEUED)
    archive_url = Mock(return_value='https://archive')
    monkeypatch.setattr(download_manager.ehs, 'archive_url', archive_url)
    aria2.get_status.side_effect = [None, {'gid': 'ec00000000000123', 'status': status}]
    aria2.add_task.side_effect = ConnectionError('response lost after submission')
    with pytest.raises(ConnectionError):
        manager._start_archive(gallery)
    assert gallery.archiver == State.QUEUED
    assert gallery.archiver_task_id is None

    manager.aria2 = aria2
    manager._start_archive(gallery)
    assert gallery.archiver == State.DOWNLOADING
    assert gallery.archiver_task_id == 'ec00000000000123'
    aria2.add_task.assert_called_once_with(123, 'https://archive', '123_token.cbz')
    archive_url.assert_called_once_with(123, 'token')


def test_archive_status_error_does_not_submit_or_change_state(monkeypatch):
    manager = DownloadManager()
    aria2 = Mock()
    manager.aria2 = aria2
    aria2.get_status.side_effect = ConnectionError('offline')
    archive_url = Mock()
    monkeypatch.setattr(download_manager.ehs, 'archive_url', archive_url)
    gallery = Gallery(gid=123, token='token', archiver=State.QUEUED)
    with pytest.raises(ConnectionError):
        manager._start_archive(gallery)
    assert gallery.archiver == State.QUEUED
    assert gallery.archiver_task_id is None
    assert manager.aria2 is None
    aria2.add_task.assert_not_called()
    archive_url.assert_not_called()


@pytest.mark.parametrize('status,valid,expected', [
    ('complete', True, State.COMPLETED),
    ('complete', False, State.VALIDATION_FAILED),
    ('error', None, State.DOWNLOAD_FAILED),
    ('removed', None, State.DOWNLOAD_FAILED),
    (None, None, State.DOWNLOAD_FAILED),
    ('active', None, State.DOWNLOADING),
])
def test_archive_cleans_results_only_after_finishing(tmp_path, monkeypatch, status, valid, expected):
    monkeypatch.setattr(download_manager.config.aria2, 'local_dir', tmp_path)
    source = tmp_path / '1_token.cbz'
    source.write_bytes(b'original archive')
    manager = DownloadManager()
    manager.aria2 = Mock()
    manager.qbit = None
    manager.aria2.get_status.return_value = {'status': status} if status else None
    monkeypatch.setattr(download_manager, 'validate', Mock(return_value=valid))
    gallery = Gallery(gid=1, token='token', archiver=State.DOWNLOADING,
                      archiver_task_id='abc')
    manager._check(gallery)
    manager.aria2.get_status.assert_called_once_with(1)
    assert gallery.archiver == expected
    assert source.exists()
    manager._cleanup_archive(gallery)
    assert source.exists() == (expected != State.COMPLETED)
    if expected == State.DOWNLOADING:
        manager.aria2.remove_result.assert_not_called()
        assert gallery.archiver_task_id == 'abc'
    else:
        manager.aria2.remove_result.assert_called_once_with(1)
        assert gallery.archiver_task_id is None


def test_cleanup_failure_preserves_final_state_and_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(download_manager.config.aria2, 'local_dir', tmp_path)
    gallery = Gallery(gid=1, token='token', archiver=State.DOWNLOADING,
                      archiver_task_id='abc')
    session = Mock()
    session.get.return_value = gallery
    committed = []
    session.commit.side_effect = lambda: committed.append(gallery.archiver)

    @contextmanager
    def sessions():
        yield session

    aria2 = Mock()
    aria2.get_status.return_value = {'status': 'complete'}

    def fail_cleanup(task_id):
        assert committed[-1] == State.COMPLETED
        raise ConnectionError('offline')

    aria2.remove_result.side_effect = fail_cleanup
    monkeypatch.setattr(download_manager, 'session_generator', sessions)
    monkeypatch.setattr(download_manager, 'Aria2Client', Mock(return_value=aria2))
    monkeypatch.setattr(download_manager, 'QbitClient', Mock())
    monkeypatch.setattr(download_manager, 'active_gallery_ids', lambda session: [1])
    monkeypatch.setattr(download_manager, 'active_counts', lambda session: (0, 0))
    monkeypatch.setattr(download_manager, 'iter_queued_gallery_ids', lambda: iter(()))
    validate = Mock(return_value=True)
    monkeypatch.setattr(download_manager, 'validate', validate)
    manager = DownloadManager()
    manager.run()
    assert gallery.archiver == State.COMPLETED
    assert gallery.archiver_task_id == 'abc'

    aria2.remove_result.side_effect = None
    manager.run()
    assert gallery.archiver == State.COMPLETED
    assert gallery.archiver_task_id is None
    assert aria2.remove_result.call_count == 2
    validate.assert_called_once()


@pytest.mark.parametrize('age_hours,state,missing,expected', [
    (1, 'downloading', False, State.DOWNLOADING),
    (169, 'downloading', False, State.DOWNLOAD_FAILED),
    (1, 'error', False, State.DOWNLOAD_FAILED),
    (1, 'missingFiles', False, State.DOWNLOAD_FAILED),
    (1, 'downloading', True, State.DOWNLOAD_FAILED),
])
def test_torrent_checks_naive_utc_start_times(
        monkeypatch, local_timezone, age_hours, state, missing, expected):
    monkeypatch.setattr(download_manager.config.qbit, 'timeout_hours', 168)
    manager = DownloadManager()
    manager.qbit = Mock()
    manager.qbit.matches_directory.return_value = True
    torrent = SimpleNamespace(hash='abc', progress=0.5, state=state)
    manager.qbit.get_torrents_info.return_value = [] if missing else [torrent]
    gallery = Gallery(
        gid=1, token='token', bittorrent=State.DOWNLOADING, bittorrent_task_id='abc',
        bittorrent_started_at=(datetime.now(timezone.utc).replace(tzinfo=None)
                               - timedelta(hours=age_hours)),
    )
    manager._check_torrent(gallery)
    assert gallery.bittorrent == expected
    if expected == State.DOWNLOAD_FAILED:
        manager.qbit.delete_torrents.assert_called_once_with(['abc'], delete_files=True)
    else:
        manager.qbit.delete_torrents.assert_not_called()


def test_torrent_submission_records_naive_utc_start_time(local_timezone):
    manager = DownloadManager()
    manager.qbit = Mock()
    manager.qbit.add_torrent.return_value = 'abc'
    manager.aria2 = None
    gallery = Gallery(gid=1, token='token', rating=400, updated_at=datetime(2020, 1, 1), bittorrent=State.QUEUED)
    gallery.torrents = [Torrent(infohash='abc')]
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    manager._enqueue(gallery, 0, 0)
    assert gallery.bittorrent_started_at.tzinfo is None
    assert before <= gallery.bittorrent_started_at <= datetime.now(timezone.utc).replace(tzinfo=None)

    engine = create_engine('sqlite://')
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(gallery)
            session.commit()
            session.expunge_all()
            gallery = session.get(Gallery, 1)
            manager.qbit.get_torrents_info.return_value = [
                SimpleNamespace(hash='abc', progress=0.5, state='downloading'),
            ]
            manager.qbit.matches_directory.return_value = True
            manager._check_torrent(gallery)
            assert gallery.bittorrent == State.DOWNLOADING
            manager.qbit.delete_torrents.assert_not_called()
    finally:
        engine.dispose()


@pytest.mark.parametrize('include_expunged', [False, True])
@pytest.mark.parametrize('expunged', [False, True, None])
@pytest.mark.parametrize('state', [State.QUEUED, State.DOWNLOAD_FAILED])
def test_enqueue_respects_expunged_config(monkeypatch, include_expunged, expunged, state):
    monkeypatch.setattr(download_manager.config.eh, 'include_expunged', include_expunged)
    manager = DownloadManager()
    manager.qbit = Mock()
    manager.aria2 = Mock()
    manager._start_archive = Mock()
    gallery = Gallery(gid=1, token='token', rating=400, expunged=expunged,
                      bittorrent=state, archiver=State.QUEUED)
    gallery.torrents = [Torrent(infohash='abc')]

    manager._enqueue(gallery, 0, 0)

    eligible = not expunged or include_expunged
    assert manager.qbit.add_torrent.called == (eligible and state == State.QUEUED)
    assert manager._start_archive.called == (eligible and state == State.DOWNLOAD_FAILED)
    if not eligible:
        assert gallery.bittorrent == state
        assert gallery.archiver == State.QUEUED
        assert gallery.bittorrent_task_id is None
        assert gallery.bittorrent_started_at is None
        assert gallery.archiver_task_id is None


@pytest.mark.parametrize('rating', [None, 400, 401, 500])
@pytest.mark.parametrize('state', [State.QUEUED, State.DOWNLOAD_FAILED])
def test_enqueue_requires_minimum_rating(monkeypatch, rating, state):
    monkeypatch.setattr(download_manager.config.filter, 'min_rating', 4.01)
    manager = DownloadManager()
    manager.qbit = Mock()
    manager.aria2 = Mock()
    manager._start_archive = Mock()
    gallery = Gallery(gid=1, token='token', rating=rating, updated_at=datetime(2020, 1, 1), bittorrent=state)
    gallery.torrents = [Torrent(infohash='abc')]
    manager._enqueue(gallery, 0, 0)
    assert manager.qbit.add_torrent.called == (rating in (401, 500) and state == State.QUEUED)
    assert manager._start_archive.called == (rating in (401, 500) and state == State.DOWNLOAD_FAILED)
    if rating not in (401, 500):
        assert gallery.bittorrent == state


@pytest.mark.parametrize('tags,eligible', [
    ([], False),
    (['language:english'], False),
    (['other:uncensored'], False),
    (['temp:english', 'other:uncensored'], False),
    (['language:English', 'other:uncensored'], False),
    (['other:uncensored', 'language:english', 'artist:someone'], True),
])
@pytest.mark.parametrize('state', [State.QUEUED, State.DOWNLOAD_FAILED])
def test_enqueue_requires_all_include_tags_without_marking_skipped_galleries(
        monkeypatch, tags, eligible, state):
    monkeypatch.setattr(download_manager.config.filter, 'include_tags',
                        ['language:english', 'other:uncensored'])
    manager = DownloadManager()
    manager.qbit = Mock()
    manager.aria2 = Mock()
    manager._start_archive = Mock()
    gallery = Gallery(gid=1, token='token', rating=400, updated_at=datetime(2020, 1, 1), bittorrent=state,
                      archiver=State.QUEUED)
    gallery.tags = [Tag.from_str(tag) for tag in tags]
    gallery.torrents = [Torrent(infohash='abc')]

    manager._enqueue(gallery, 0, 0)

    assert manager.qbit.add_torrent.called == (eligible and state == State.QUEUED)
    assert manager._start_archive.called == (eligible and state == State.DOWNLOAD_FAILED)
    if not eligible:
        assert gallery.bittorrent == state
        assert gallery.archiver == State.QUEUED
        assert gallery.bittorrent_task_id is None
        assert gallery.bittorrent_started_at is None
        assert gallery.archiver_task_id is None
        assert gallery.dupe_with is None


def test_source_cleanup_failure_retries_without_revalidating(tmp_path, monkeypatch):
    monkeypatch.setattr(download_manager.config.aria2, 'local_dir', tmp_path)
    source = tmp_path / '1_token.cbz'
    source.write_bytes(b'original archive')
    manager = DownloadManager()
    manager.aria2 = Mock()
    gallery = Gallery(gid=1, token='token', archiver=State.COMPLETED, archiver_task_id='abc')
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', Mock(side_effect=PermissionError('busy')))
        with pytest.raises(PermissionError):
            manager._cleanup_archive(gallery)
    assert gallery.archiver == State.COMPLETED
    assert gallery.archiver_task_id == 'abc'
    manager.aria2.remove_result.assert_not_called()
    manager._cleanup_archive(gallery)
    assert not source.exists()
    assert gallery.archiver_task_id is None


@pytest.mark.parametrize('enabled,age_minutes,checked_minutes,eligible', [
    (False, 120, None, True),
    (False, 30, None, True),
    (False, None, None, True),
    (False, 60, None, True),
    (True, 120, None, False),
    (True, 120, 121, False),
    (True, 120, 1, True),
    (True, 30, None, False),
    (True, 30, 31, False),
    (True, 30, 1, True),
    (True, None, 1, False),
])
def test_enqueue_requires_current_dedupe_check_without_age_gate(
        monkeypatch, enabled, age_minutes, checked_minutes, eligible):
    monkeypatch.setattr(download_manager.config.filter.dedupe, 'enabled', enabled)
    monkeypatch.setattr(download_manager.config.filter, 'not_before_min', 60)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    gallery = Gallery(
        gid=1, token='a', rating=400, bittorrent=State.DOWNLOAD_FAILED,
        posted_at=now - timedelta(days=1),
        updated_at=None if age_minutes is None else now - timedelta(minutes=age_minutes),
        deduped_at=None if checked_minutes is None else now - timedelta(minutes=checked_minutes),
    )
    manager = DownloadManager()
    manager.qbit = None
    manager.aria2 = Mock()
    manager._start_archive = Mock()
    manager._enqueue(gallery, 0, 0)
    assert manager._start_archive.called == eligible
