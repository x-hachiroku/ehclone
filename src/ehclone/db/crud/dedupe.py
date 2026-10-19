from collections.abc import Iterator
from datetime import datetime, timezone
from math import ceil

from sqlalchemy import text
from sqlalchemy.orm import Query, Session, joinedload

from ehclone.config import config
from ehclone.db.entities import Gallery, Thumb
from ehclone.db.session import session_generator


BATCH_SIZE = 10000


def build_base_query(session):
    """All eligible current winners, including previously checked galleries."""
    return session.query(Gallery).join(
        Thumb, Gallery.thumb_url == Thumb.url,
    ).filter(
        Gallery.title.isnot(None),
        Gallery.filecount.isnot(None),
        Thumb.mobile_net_v3.isnot(None),
        Thumb.not_found.is_(False),
        Gallery.rating * 0.01 >= config.filter.min_rating,
        Gallery.updated_at.isnot(None),
        Gallery.dupe_with.is_(None),
    )


def build_pending_query(session: Session) -> Query[Gallery]:
    """Bases needing their first comparison or a check of refreshed metadata."""
    return build_base_query(session).filter(
        Gallery.deduped_at.is_(None) | (Gallery.deduped_at < Gallery.updated_at),
    )


def iter_base_candidates() -> Iterator[tuple[int, str]]:
    """Yield candidates in GID order, closing each page's session before yielding."""
    after_gid = None
    while True:
        with session_generator() as session:
            query = build_pending_query(session).with_entities(Gallery.gid, Gallery.token)
            if after_gid is not None:
                query = query.filter(Gallery.gid > after_gid)
            rows = query.order_by(Gallery.gid).limit(BATCH_SIZE).all()
        if not rows:
            return
        after_gid = rows[-1][0]
        yield from rows


def fetch_dupe_pool(session: Session, base_gallery: Gallery, vector: list[float]) -> list[Gallery]:
    """Find bounded nearest candidates, then enforce the tag-dependent page tolerance."""
    if base_gallery.filecount is None:
        return []
    dedupe_cfg = config.filter.dedupe
    page_tolerance = max(
        dedupe_cfg.page_tolerance,
        ceil(base_gallery.filecount * dedupe_cfg.page_tolerance_rate),
    )
    # Search the full range without tag lookups in the nearest-neighbor query.
    query_tolerance = page_tolerance + dedupe_cfg.page_tolerance_ad
    if session.get_bind().dialect.name == 'postgresql':
        # Transaction-local settings avoid leaking into other pooled queries.
        # Iterative scans keep searching when gallery filters discard neighbors.
        session.execute(text(f"SET LOCAL hnsw.ef_search = {config.filter.dedupe.candidate_limit * 2}"))
        session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))

    distance = Thumb.mobile_net_v3.cosine_distance(vector)
    nearest = build_base_query(session).with_entities(
        Gallery.gid, distance.label('distance'),
    ).filter(
        Gallery.gid != base_gallery.gid,
        Gallery.filecount > base_gallery.filecount - query_tolerance,
        Gallery.filecount < base_gallery.filecount + query_tolerance,
    ).order_by(distance).limit(config.filter.dedupe.candidate_limit).cte('nearest').prefix_with(
        'MATERIALIZED', dialect='postgresql',
    )
    # Keep the distance threshold outside the materialized nearest-neighbor query
    # so iterative scans do not keep searching to fill the limit with rare dupes.
    candidates = session.query(Gallery).join(nearest, Gallery.gid == nearest.c.gid).options(
        joinedload(Gallery.tags),
    ).filter(
        nearest.c.distance < config.filter.dedupe.distance_threshold,
    ).order_by(nearest.c.distance, Gallery.gid).all()
    base_has_ads = any(
        tag.namespace == 'other' and tag.name == 'extraneous ads' for tag in base_gallery.tags
    )
    return [
        candidate for candidate in candidates
        if abs(candidate.filecount - base_gallery.filecount) < page_tolerance
        or base_has_ads
        or any(tag.namespace == 'other' and tag.name == 'extraneous ads' for tag in candidate.tags)
    ]


def expand_dupe_links(session, gid_set):
    """Load descendants for redirection, without ranking old duplicates."""
    found = {}
    pending = set(gid_set)
    visited = set()
    while pending:
        visited.update(pending)
        galleries = session.query(Gallery).filter(Gallery.dupe_with.in_(pending)).all()
        pending = set()
        for gallery in galleries:
            found[gallery.gid] = gallery
            pending.add(gallery.gid)
        pending -= visited
    return list(found.values())


def persist_dedupe_resolution(session, winner_gid, galleries, checked_gid: int):
    checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    for gallery in galleries:
        gallery.dupe_with = None if gallery.gid == winner_gid else winner_gid
        # Other winners may still need their own comparison: similarity is
        # not transitive, so matching this base does not finish their checks.
        if gallery.gid == checked_gid:
            gallery.deduped_at = checked_at
    session.flush()
