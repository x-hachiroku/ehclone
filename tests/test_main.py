from contextlib import contextmanager
import time
from unittest.mock import Mock

import pytest
import sqlalchemy_utils

from ehclone import main
from ehclone.core import download_manager, dedupe, sync_index, sync_thumbs
from ehclone.db import locking, session
from ehclone.db.entities import Base


@pytest.mark.parametrize('acquired', [False, True])
def test_jobs_run_only_while_application_lock_is_held(monkeypatch, acquired):
    held = False
    jobs = []

    @contextmanager
    def lock():
        nonlocal held
        held = acquired
        try:
            yield acquired
        finally:
            held = False

    @contextmanager
    def begin():
        assert held
        yield Mock()

    def job(name):
        def run():
            assert held
            jobs.append(name)
        return run

    class StopLoop(Exception):
        pass

    def sleep(seconds):
        assert held
        assert seconds == 300
        raise StopLoop

    monkeypatch.setattr(time, 'sleep', sleep)
    monkeypatch.setattr(sqlalchemy_utils, 'database_exists', lambda url: True)
    monkeypatch.setattr(locking, 'application_lock', lock)
    monkeypatch.setattr(session.engine, 'begin', begin)
    monkeypatch.setattr(Base.metadata, 'create_all', Mock())
    monkeypatch.setattr(sync_index, 'run', job('index'))
    monkeypatch.setattr(sync_thumbs, 'run', job('thumbs'))
    monkeypatch.setattr(dedupe, 'run', job('dedupe'))
    monkeypatch.setattr(download_manager, 'run', job('downloads'))
    if acquired:
        with pytest.raises(StopLoop):
            main.main()
    else:
        main.main()
    assert not held
    assert jobs == (['downloads', 'index', 'thumbs', 'dedupe'] if acquired else [])
