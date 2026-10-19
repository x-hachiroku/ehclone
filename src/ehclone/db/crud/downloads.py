"""Queries for the two persisted download queues."""

from collections.abc import Iterator

from sqlalchemy import func, select

from ehclone.config import config
from ehclone.db.entities import DownloadState as State, Gallery, Tag
from ehclone.db.session import session_generator


BATCH_SIZE = 10000


def active_gallery_ids(session) -> list[int]:
    query = select(Gallery.gid).where(
        (Gallery.bittorrent == State.DOWNLOADING)
        | (Gallery.archiver == State.DOWNLOADING)
        | (Gallery.archiver.in_([State.COMPLETED, State.DOWNLOAD_FAILED, State.VALIDATION_FAILED])
        & Gallery.archiver_task_id.isnot(None))
    )
    return list(session.scalars(query.order_by(Gallery.gid.desc())))


def iter_queued_gallery_ids() -> Iterator[int]:
    """Yield queued IDs lowest first without holding a session during processing."""
    query = select(Gallery.gid).where(
        Gallery.dupe_with.is_(None),
        Gallery.title.isnot(None),
        Gallery.rating * 0.01 >= config.filter.min_rating,
        Gallery.bittorrent.notin_([State.COMPLETED, State.DOWNLOADING]),
        Gallery.archiver == State.QUEUED,
    )
    if not config.eh.include_expunged:
        query = query.where(Gallery.expunged.is_not(True))
    if config.filter.include_categories:
        query = query.where(Gallery.category.in_(config.filter.include_categories))
    if config.filter.dedupe.enabled:
        query = query.where(
            Gallery.deduped_at.isnot(None),
            Gallery.updated_at.isnot(None),
        )
    for tag_string in config.filter.include_tags:
        tag = Tag.from_str(tag_string)
        query = query.where(Gallery.tags.any(
            (Tag.namespace == tag.namespace) & (Tag.name == tag.name)
        ))
    for tag_string in config.filter.exclude_tags:
        tag = Tag.from_str(tag_string)
        query = query.where(~Gallery.tags.any(
            (Tag.namespace == tag.namespace) & (Tag.name == tag.name)
        ))
    after_gid = None
    while True:
        page = query
        if after_gid is not None:
            page = page.where(Gallery.gid > after_gid)
        with session_generator() as session:
            ids = list(session.scalars(page.order_by(Gallery.gid.asc()).limit(BATCH_SIZE)))
        if not ids:
            return
        after_gid = ids[-1]
        yield from ids


def active_counts(session) -> tuple[int, int]:
    return tuple(session.scalar(select(func.count()).select_from(Gallery).where(
        column == State.DOWNLOADING
    )) for column in (Gallery.bittorrent, Gallery.archiver))
