from datetime import datetime, timezone

from sqlalchemy import func

from ehclone.logger import logger
from ehclone.db.entities import Gallery, Tag, Torrent, Category
from ehclone.db.session import session_generator


def gallery_from_dict(data):
    try:
        gid = int(data['gid'])
        posted_ts = int(data['posted'])
        posted_dt = datetime.fromtimestamp(posted_ts, tz=timezone.utc)
        category = Category(data['category'])

        uploader = data.get('uploader')
        if uploader == '(Disowned)':
            uploader = None

        rating = data.get('rating', 0)
        rating = round(float(rating) * 20)

        gallery_dict = {
            'gid': gid,
            'token': data['token'],
            'title': data['title'],
            'title_jpn': data.get('title_jpn'),
            'category': category,
            'thumb': data.get('thumb'),
            'uploader': uploader,
            'posted': posted_dt,
            'filecount': int(data['filecount']),
            'filesize': int(data['filesize']),
            'expunged': data.get('expunged', False),
            'rating': rating,
            'current_gid': int(data['current_gid']) if data.get('current_gid') else None,
            'downloaded': 0
        }

        related = []
        for prefix in ['parent', 'current', 'first']:
            p_gid = data.get(f'{prefix}_gid')
            p_key = data.get(f'{prefix}_key')
            if p_gid:
                p_gid = int(p_gid)
                if p_gid != gid:
                    if not p_key:
                        raise ValueError(f'Missing token for related gallery {p_gid}')
                    related.append({'gid': p_gid, 'token': p_key})

        tags = []
        for tag_str in data.get('tags', []):
            if ':' not in tag_str:
                continue
            namespace, name = tag_str.split(':', 1)
            tags.append({'namespace': namespace, 'name': name})

        torrents = []
        for t_data in data.get('torrents', []):
            added_ts = int(t_data['added'])
            added_dt = datetime.fromtimestamp(added_ts, tz=timezone.utc)
            torrents.append({
                'gid': gid,
                'infohash': t_data['hash'],
                'added': added_dt,
                'name': t_data.get('name'),
                'tsize': int(t_data['tsize']) if t_data.get('tsize') else None,
                'fsize': int(t_data['fsize']) if t_data.get('fsize') else None
            })

        return gallery_dict, related, tags, torrents

    except (ValueError, TypeError, KeyError) as e:
        raise ValueError(f'Error processing gallery {data.get("gid", "unknown")}: {e}') from e


def insert_galleries(galleries_data):
    processed_items = []
    for data in galleries_data:
        try:
            processed_items.append(gallery_from_dict(data))
        except ValueError as e:
            logger.error(f'Failed to process gallery data: {e}')
            continue

    with session_generator() as session:
        tag_cache = {}

        for gallery_dict, related, tags_data, torrents_data in processed_items:
            for r in related:
                r_gid = r['gid']
                if not session.query(Gallery).get(r_gid):
                    session.add(Gallery(gid=r_gid, token=r['token']))

            gallery = Gallery(**gallery_dict)
            gallery = session.merge(gallery)

            current_tags = []
            for t in tags_data:
                key = (t['namespace'], t['name'])
                if key in tag_cache:
                    tag = tag_cache[key]
                else:
                    tag = session.query(Tag).filter_by(namespace=t['namespace'], name=t['name']).first()
                    if not tag:
                        tag = Tag(namespace=t['namespace'], name=t['name'])
                        session.add(tag)
                    tag_cache[key] = tag
                current_tags.append(tag)
            gallery.tags = current_tags

            new_torrents = [Torrent(**t) for t in torrents_data]
            gallery.torrents = new_torrents


def get_last_gid(expunged=None):
    with session_generator() as session:
        query = session.query(func.max(Gallery.gid))
        query = query.filter(Gallery.title.isnot(None))
        if expunged is not None:
            query = query.filter(Gallery.expunged == expunged)
        last_gid = query.scalar()
    if last_gid is None:
        return 1
    return last_gid
