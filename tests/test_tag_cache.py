from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from ehclone.db.crud import gallery
from ehclone.db.entities import Base, Gallery, Tag


@pytest.fixture
def database(monkeypatch):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with engine.connect() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')

    @contextmanager
    def sessions():
        with Session(engine, expire_on_commit=False) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    monkeypatch.setattr(gallery, 'session_generator', sessions)
    yield engine
    engine.dispose()


def metadata(gid, tags):
    return dict(gid=gid, token='a', title='Title', posted='1767225600',
                category='Manga', filecount='10', filesize='100', rating='4', tags=tags)


def test_ingestion_reuses_tags_across_batches_and_ignores_duplicates(database):
    with Session(database) as session:
        session.add(Tag(namespace='language', name='english'))
        session.commit()

    tags = ['language:english', 'other:uncensored', 'other:uncensored']
    gallery.insert_galleries([metadata(1, tags), metadata(2, tags)])
    gallery.insert_galleries([metadata(3, tags)])

    with Session(database) as session:
        assert session.query(Tag).count() == 2
        for gid in (1, 2, 3):
            assert {(tag.namespace, tag.name) for tag in session.get(Gallery, gid).tags} == {
                ('language', 'english'), ('other', 'uncensored'),
            }


@pytest.mark.parametrize('prior_batch', [False, True])
@pytest.mark.parametrize('failure_stage', ['batch', 'commit'])
def test_failed_batch_rolls_back_tags_and_can_be_retried(database, monkeypatch, prior_batch, failure_stage):
    if prior_batch:
        gallery.insert_galleries([metadata(1, ['language:english'])])

    with monkeypatch.context() as patch:
        if failure_stage == 'batch':
            insert = gallery.insert_gallery

            def fail_after_insert(*args):
                insert(*args)
                raise RuntimeError('batch failed')

            patch.setattr(gallery, 'insert_gallery', fail_after_insert)
        else:
            sessions = gallery.session_generator

            @contextmanager
            def fail_on_commit():
                with sessions() as session:
                    def fail(session):
                        raise RuntimeError('commit failed')

                    event.listen(session, 'before_commit', fail)
                    yield session

            patch.setattr(gallery, 'session_generator', fail_on_commit)

        with pytest.raises(RuntimeError, match=f'{failure_stage} failed'):
            gallery.insert_galleries([metadata(2, ['other:uncensored'])])

    with Session(database) as session:
        assert session.get(Gallery, 2) is None
        assert session.query(Tag).filter_by(namespace='other', name='uncensored').first() is None

    gallery.insert_galleries([metadata(2, ['other:uncensored'])])
    with Session(database) as session:
        tag = session.get(Gallery, 2).tags[0]
        assert (tag.namespace, tag.name) == ('other', 'uncensored')


def test_refresh_replaces_tags_and_expires_loaded_relationship(database):
    gallery.insert_galleries([metadata(1, ['english', 'temp:english', 'language:english'])])
    with Session(database, expire_on_commit=False) as session:
        item = session.get(Gallery, 1)
        assert len(item.tags) == 2
        gallery.insert_gallery(session, metadata(1, ['other:uncensored']), {}, {})
        assert [(tag.namespace, tag.name) for tag in item.tags] == [('other', 'uncensored')]
        gallery.insert_gallery(session, metadata(1, []), {}, {})
        assert item.tags == []
        session.commit()
        # Refreshing relationships must not delete reusable tag records.
        assert session.query(Tag).count() == 3


def test_ingestion_reuses_tag_inserted_between_batches(database):
    gallery.insert_galleries([metadata(1, [])])
    with Session(database) as session:
        session.add(Tag(namespace='artist', name='new'))
        session.commit()
        tag_id = session.scalar(select(Tag.id))

    gallery.insert_galleries([metadata(2, ['artist:new'])])
    with Session(database) as session:
        assert session.query(Tag).count() == 1
        assert session.get(Gallery, 2).tags[0].id == tag_id


def test_ingestion_keeps_tag_identities_separate_between_databases(database, monkeypatch):
    gallery.insert_galleries([metadata(1, ['language:english'])])
    with Session(database) as session:
        original_id = session.get(Gallery, 1).tags[0].id
    other_engine = create_engine('sqlite://')
    try:
        Base.metadata.create_all(other_engine)
        with Session(other_engine) as session:
            session.add(Tag(id=50, namespace='language', name='english'))
            session.commit()

        @contextmanager
        def sessions():
            with Session(other_engine) as session:
                yield session
                session.commit()

        with monkeypatch.context() as patch:
            patch.setattr(gallery, 'session_generator', sessions)
            gallery.insert_galleries([metadata(1, ['language:english'])])
        with Session(other_engine) as session:
            assert session.get(Gallery, 1).tags[0].id == 50

        gallery.insert_galleries([metadata(2, ['language:english'])])
        with Session(database) as session:
            assert session.get(Gallery, 2).tags[0].id == original_id
    finally:
        other_engine.dispose()
