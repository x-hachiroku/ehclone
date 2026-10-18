from collections import ChainMap
from weakref import WeakKeyDictionary
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlalchemy.orm import selectinload

from ehclone.config import config
from ehclone.logger import gallery_logger
from ehclone.db.entities import Gallery, Tag, Torrent, Category, Thumb, gallery_tag
from ehclone.db.session import session_generator


# Committed IDs only; separate databases must never share tag identities.
# The application lock serializes ingestion, and tags are never deleted or renamed.
_tag_caches: WeakKeyDictionary[Engine, dict[tuple[str, str], int]] = WeakKeyDictionary()


def get_gallery_metadata(gid: int) -> Gallery | None:
    """Load gallery fields and tags for use after the read session closes."""
    with session_generator() as session:
        return session.get(Gallery, gid, options=[selectinload(Gallery.tags)])


def get_existing_gids(gids: list[int]) -> set[int]:
    """Find stored gallery IDs, including entries without metadata."""
    with session_generator() as session:
        rows = session.query(Gallery.gid).filter(Gallery.gid.in_(gids)).all()
        return {gid for gid, in rows}


def insert_gallery(session, gdata, thumb_cache, tag_cache):
    try:
        gid = int(gdata['gid'])
        posted_ts = int(gdata['posted'])
        posted_dt = datetime.fromtimestamp(posted_ts, tz=timezone.utc).replace(tzinfo=None)
        category = Category(gdata['category'])
        updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if updated_at - posted_dt < timedelta(minutes=config.filter.not_before_min):
            updated_at = None

        uploader = gdata.get('uploader')
        if uploader == '(Disowned)':
            uploader = None

        rating = gdata.get('rating', 0)
        rating = round(float(rating) * 100) # rating are precise to 2 decimal places so there should be no loss of precision.

        # First GID must be lower than current GID, so this shouldn't break get_last_gid.
        first_gid = gdata.get('first_gid')
        if first_gid:
            first_gid = int(first_gid)
            first_token = gdata['first_key']

        gallery = Gallery(
            gid = gid,
            token = gdata['token'],
            title = gdata['title'],
            title_jpn = gdata.get('title_jpn'),
            category = category,
            thumb_url = gdata.get('thumb'),
            uploader = uploader,
            posted_at = posted_dt,
            filecount = int(gdata['filecount']),
            filesize = int(gdata['filesize']),
            expunged = gdata.get('expunged'),
            rating = rating,
            first_gid = first_gid,
            updated_at = updated_at,
        )

        tags = []
        for tag_str in gdata.get('tags', []):
            tags.append(Tag.from_str(tag_str))

        torrents = []
        for t_data in gdata.get('torrents', []):
            added_ts = int(t_data['added'])
            added_dt = datetime.fromtimestamp(added_ts, tz=timezone.utc).replace(tzinfo=None)
            torrents.append(Torrent(
                gid = gid,
                infohash = t_data['hash'],
                added_at = added_dt,
                name = t_data.get('name'),
                tsize = int(t_data['tsize']),
                fsize = int(t_data['fsize']),
            ))

    except ValueError as e:
        log = gallery_logger(gdata.get('gid'), gdata.get('token'))
        log.error('Failed to process gallery data')
        log.error(gdata)
        log.exception(e)
        raise

    thumb = None
    if gallery.thumb_url:
        if gallery.thumb_url in thumb_cache:
            thumb = thumb_cache[gallery.thumb_url]
        else:
            thumb = session.get(Thumb, gallery.thumb_url)
            if not thumb:
                thumb = Thumb(url=gallery.thumb_url)
                session.add(thumb)
            thumb_cache[gallery.thumb_url] = thumb

    tag_ids = set()
    for _tag in tags:
        key = (_tag.namespace, _tag.name)
        if key in tag_cache:
            tag_id = tag_cache[key]
        else:
            tag_id = session.query(Tag.id).filter_by(
                namespace=_tag.namespace, name=_tag.name,
            ).scalar()
            if tag_id is None:
                session.add(_tag)
                session.flush()
                tag_id = _tag.id
            tag_cache[key] = tag_id
        tag_ids.add(tag_id)

    if first_gid:
        first_gallery = session.get(Gallery, first_gid)
        if first_gallery is None:
            first_gallery = Gallery(gid=first_gid, token=first_token)
            session.add(first_gallery)
        first_gallery.first_gid = first_gid

    gallery = session.merge(gallery)

    if first_gid:
        chain = (session
                 .query(Gallery)
                 .filter_by(first_gid=first_gid)
                 .order_by(Gallery.gid)).all()
        chain_end = max(chain[-1].gid, gid)
        chain_ids = {member.gid for member in chain}
        for g in chain:
            if g.gid < chain_end:
                g.dupe_with = chain_end
            elif g.dupe_with in chain_ids:
                g.dupe_with = None

    # Keep previously seen torrents even if they disappear from later metadata.
    for torrent in torrents:
        session.merge(torrent)
    # Write IDs directly so cache hits do not load Tag objects in each session.
    session.flush()
    session.execute(gallery_tag.delete().where(gallery_tag.c.gallery_gid == gid))
    if tag_ids:
        session.execute(gallery_tag.insert(), [
            {'gallery_gid': gid, 'tag_id': tag_id} for tag_id in sorted(tag_ids)
        ])
    session.expire(gallery, ['tags', 'torrents'])


def insert_galleries(gdata_list):
    with session_generator() as session:
        bind = session.get_bind()
        committed_tags = _tag_caches.get(bind)
        if committed_tags is None:
            committed_tags = {
                (namespace, name): tag_id
                for namespace, name, tag_id in session.query(Tag.namespace, Tag.name, Tag.id)
            }
        new_tags = {}
        tag_cache = ChainMap(new_tags, committed_tags)
        thumb_cache = {}
        for gdata in gdata_list:
            insert_gallery(session, gdata, thumb_cache, tag_cache)
    # session_generator commits on exit. Failed batches must not publish IDs.
    committed_tags.update(new_tags)
    _tag_caches[bind] = committed_tags


def get_galleries_for_metadata_refresh(limit: int) -> list[list[int | str]]:
    """Refresh pending metadata once the gallery reaches the filtering age."""
    minimum_age = timedelta(minutes=config.filter.not_before_min)
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - minimum_age
    with session_generator() as session:
        rows = session.query(Gallery.gid, Gallery.token).filter(
            Gallery.title.isnot(None),
            Gallery.posted_at <= cutoff,
            Gallery.updated_at.is_(None),
        ).order_by(Gallery.gid).limit(limit).all()
        return [[gid, token] for gid, token in rows]


def get_last_gid(expunged=None):
    with session_generator() as session:
        query = session.query(func.max(Gallery.gid))
        query = query.filter(Gallery.title.isnot(None))
        if config.eh.categories:
            query = query.filter(Gallery.category.in_(config.eh.categories))
        if expunged is not None:
            query = query.filter(Gallery.expunged == expunged)
        last_gid = query.scalar()
    if last_gid is None:
        return 1
    return last_gid
