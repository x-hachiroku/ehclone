from collections.abc import Iterator

from ehclone.config import config
from ehclone.db.entities import Thumb, Gallery, Tag
from ehclone.db.session import session_generator


BATCH_SIZE = 128


def iter_unvectorized_thumb_batches() -> Iterator[list[str]]:
    """Page by gallery GID and yield unique URLs per page outside the read session."""
    after_gid = None
    while True:
        with session_generator() as session:
            query = (
                session.query(Gallery.gid, Thumb.url)
                .select_from(Gallery)
                .join(Thumb, Gallery.thumb_url == Thumb.url)
                .filter(
                    Thumb.mobile_net_v3.is_(None),
                    Thumb.not_found.is_(False),
                    Gallery.rating * 0.01 >= config.filter.min_rating,
                )
            )
            for tag_string in config.filter.include_tags:
                tag = Tag.from_str(tag_string)
                query = query.filter(Gallery.tags.any(
                    (Tag.namespace == tag.namespace) & (Tag.name == tag.name)
                ))
            if after_gid is not None:
                query = query.filter(Gallery.gid > after_gid)
            rows = query.order_by(Gallery.gid).limit(BATCH_SIZE).all()
        if not rows:
            return
        after_gid = rows[-1][0]
        yield list(dict.fromkeys(url for gid, url in rows))


def update_thumb_vector(url, vector):
    with session_generator() as session:
        thumb = session.get(Thumb, url)
        if thumb:
            thumb.mobile_net_v3 = vector


def mark_thumb_not_found(url: str) -> None:
    with session_generator() as session:
        thumb = session.get(Thumb, url)
        if thumb:
            thumb.not_found = True
