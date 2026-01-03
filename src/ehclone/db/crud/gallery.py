from datetime import datetime, timezone

from sqlalchemy import func

from ehclone.logger import logger
from ehclone.db.entities import Gallery, Tag, Torrent, Category, Thumb
from ehclone.db.session import session_generator


def insert_gallery(session, gdata, thumb_cache, tag_cache):
    try:
        gid = int(gdata['gid'])
        posted_ts = int(gdata['posted'])
        posted_dt = datetime.fromtimestamp(posted_ts, tz=timezone.utc)
        category = Category(gdata['category'])

        uploader = gdata.get('uploader')
        if uploader == '(Disowned)':
            uploader = None

        rating = gdata.get('rating', 0)
        rating = round(float(rating) * 20)

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
        )

        tags = []
        for tag_str in gdata.get('tags', []):
            tags.append(Tag.from_str(tag_str))

        torrents = []
        for t_data in gdata.get('torrents', []):
            added_ts = int(t_data['added'])
            added_dt = datetime.fromtimestamp(added_ts, tz=timezone.utc)
            torrents.append(Torrent(
                gid = gid,
                infohash = t_data['hash'],
                added_at = added_dt,
                name = t_data.get('name'),
                tsize = int(t_data['tsize']),
                fsize = int(t_data['fsize']),
            ))

    except ValueError as e:
        logger.error('Failed to process gallery data')
        logger.error(gdata)
        logger.exception(e)
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

    gallery_tags = []
    for _tag in tags:
        key = (_tag.namespace, _tag.name)
        if key in tag_cache:
            tag = tag_cache[key]
        else:
            tag = session.query(Tag).filter_by(namespace=_tag.namespace, name=_tag.name).first()
            if not tag:
                tag = _tag
                session.add(tag)
            tag_cache[key] = tag
        gallery_tags.append(tag)

    if first_gid:
        first_gallery = session.get(Gallery, first_gid)
        if first_gallery is None:
            first_gallery = Gallery(gid=first_gid, token=first_token)
            first_gallery.first_gid = first_gid
            session.add(first_gallery)

    gallery = session.merge(gallery)

    if first_gid:
        chain = (session
                 .query(Gallery)
                 .filter_by(first_gid=first_gid)
                 .order_by(Gallery.gid)).all()
        chain_end = max(chain[-1].gid, gid)
        for g in chain:
            if g.gid < chain_end:
                g.dupe_with = chain_end

    gallery.tags = gallery_tags
    gallery.torrents = torrents


def insert_galleries(gdata_list):
    with session_generator() as session:
        thumb_cache = {}
        tag_cache = {}
        for gdata in gdata_list:
            insert_gallery(session, gdata, thumb_cache, tag_cache)


def get_last_gid(categories=None, expunged=None):
    with session_generator() as session:
        query = session.query(func.max(Gallery.gid))
        query = query.filter(Gallery.title.isnot(None))
        if categories:
            query = query.filter(Gallery.category.in_(categories))
        if expunged is not None:
            query = query.filter(Gallery.expunged == expunged)
        last_gid = query.scalar()
    if last_gid is None:
        return 1
    return last_gid
