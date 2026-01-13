from sqlalchemy import func

from ehclone.db.entities import Thumb, Gallery
from ehclone.db.session import session_generator


def get_unvectorized_thumbs(limit):
    with session_generator() as session:
        subq = (
            session.query(
                Thumb.url,
                func.min(Gallery.gid).label('min_gid')
            )
            .join(Gallery, Gallery.thumb_url == Thumb.url)
            .filter(Thumb.mobile_net_v3.is_(None))
            .group_by(Thumb.url)
            .subquery()
        )
        results = (
            session.query(subq.c.url)
            .order_by(subq.c.min_gid)
            .limit(limit)
            .all()
        )
        return [r[0] for r in results]


def update_thumb_vector(url, vector):
    with session_generator() as session:
        thumb = session.get(Thumb, url)
        if thumb:
            thumb.mobile_net_v3 = vector
