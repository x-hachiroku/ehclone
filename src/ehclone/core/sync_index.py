import re
from bs4 import BeautifulSoup
from urllib.parse import urlencode

from ehclone.config import config
from ehclone.logger import gallery_logger, logger
from ehclone.core.session import ehs
from ehclone.db.crud.gallery import (
    get_existing_gids,
    get_galleries_for_metadata_refresh,
    get_last_gid,
    insert_galleries,
)


def sync_page(prev: int, expunged: bool = False, recheck_until: int | None = None) -> int:
    '''
    Get a page of galleries from EH
    :param prev: The last gallery ID from the previous page, i.e. exclusive
    :param expunged: ONLY expunged galleries when true; only not expunged when false
    :param recheck_until: Skip stored galleries up to and including this original cursor

    :return: last gallery ID, -1 if no galleries found
    '''

    _args = config.eh.get_search_args()
    _args['prev'] = prev

    if expunged:
        _args['f_sh'] = 'on'
    elif 'f_sh' in _args:
        del _args['f_sh']

    _query = urlencode(_args)

    res = ehs.get('/?' + _query)
    soup = BeautifulSoup(res.text, 'lxml')
    galleries_a = soup.select('.itg a[href]')

    gidlist = []
    for a in galleries_a:
        match = re.search(r'/g/(\d+)/([0-9a-f]+)/', a['href'])
        if match:
            entry = [int(match[1]), match[2]]
            if entry not in gidlist:
                gidlist.append(entry)
    logger.debug(f'Found {len(gidlist)} galleries: {list(map(lambda x: f"{x[0]}/{x[1]}", gidlist))}')

    if not gidlist:
        return -1

    next_gid = max(gid for gid, token in gidlist)
    recheck_gids = [gid for gid, token in gidlist
                   if recheck_until is not None and gid <= recheck_until]
    if recheck_gids:
        existing_gids = get_existing_gids(recheck_gids)
        gidlist = [[gid, token] for gid, token in gidlist
                   if gid > recheck_until or gid not in existing_gids]
        for gid, token in gidlist:
            if gid <= recheck_until:
                gallery_logger(gid, token).warning('Gallery missing from database during recheck; fetching metadata')

    if gidlist:
        _gdata = ehs.gdata(gidlist)
        if not _gdata:
            raise RuntimeError('Could not retrieve gallery metadata.')
        logger.debug(f'Retrieved metadata for {len(_gdata)} galleries: {list(map(lambda x: f"{x["gid"]}/{x["token"]}", _gdata))}')

        insert_galleries(_gdata)

    return next_gid if next_gid > prev else -1


def run() -> None:
    recheck = config.eh.recheck > 0
    original_last_gid = get_last_gid(expunged=False)
    last_gid = min(config.eh.recheck, original_last_gid) if recheck else original_last_gid
    logger.info(f'Starting sync from {last_gid}')

    if config.eh.include_expunged:
        original_expunged_gid = get_last_gid(expunged=True)
        expunged_gid = min(config.eh.recheck, original_expunged_gid) if recheck else original_expunged_gid
        logger.info(f'Starting expunged sync from {expunged_gid}')

        while True:
            logger.info(f'Syncing expunged page with prev={expunged_gid}')
            new_gid = sync_page(expunged_gid, expunged=True,
                                recheck_until=original_expunged_gid if recheck else None)
            if new_gid == -1:
                logger.info('Expunged sync complete, no more galleries found.')
                break

            expunged_gid = new_gid

    while True:
        logger.info(f'Syncing page with prev={last_gid}')
        new_gid = sync_page(last_gid, expunged=False,
                            recheck_until=original_last_gid if recheck else None)
        if new_gid == -1:
            logger.info('Sync complete, no more galleries found.')
            break

        last_gid = new_gid

    while gidlist := get_galleries_for_metadata_refresh(limit=ehs.GDATA_LIMIT):
        logger.debug('Fetched metadata refresh batch: {} galleries', len(gidlist))
        logger.info('Refreshing metadata for {} galleries past the filtering age', len(gidlist))
        gdata = ehs.gdata(gidlist)
        if not gdata:
            raise RuntimeError('Could not retrieve gallery metadata for refresh.')
        insert_galleries(gdata)


if __name__ == '__main__':
    run()
