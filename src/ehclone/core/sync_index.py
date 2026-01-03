import re
from bs4 import BeautifulSoup
from urllib.parse import urlencode

from ehclone.config import config
from ehclone.logger import logger
from ehclone.core.session import ehs
from ehclone.db.crud.gallery import (
    get_galleries_for_metadata_refresh,
    get_last_gid,
    insert_galleries,
)


def sync_page(prev, expunged=False):
    '''
    Get a page of galleries from EH
    :param prev: The last gallery ID from the previous page, i.e. exclusive
    :param expunged: ONLY expunged galleries when true; only not expunged when false

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

    _gdata = ehs.gdata(gidlist)
    if not _gdata:
        raise RuntimeError('Could not retrieve gallery metadata.')
    logger.debug(f'Retrieved metadata for {len(_gdata)} galleries: {list(map(lambda x: f"{x["gid"]}/{x["token"]}", _gdata))}')

    insert_galleries(_gdata)

    next_gid = max(gid for gid, token in gidlist)
    return next_gid if next_gid > prev else -1


def run() -> None:
    last_gid = get_last_gid(expunged=False) if config.eh.resume else 1
    logger.info(f'Starting sync from {last_gid}')

    if config.eh.include_expunged:
        expunged_gid = get_last_gid(expunged=True) if config.eh.resume else 1
        logger.info(f'Starting expunged sync from {expunged_gid}')

        while True:
            logger.info(f'Syncing expunged page with prev={expunged_gid}')
            new_gid = sync_page(expunged_gid, expunged=True)
            if new_gid == -1:
                logger.info('Expunged sync complete, no more galleries found.')
                break

            expunged_gid = new_gid

    while True:
        logger.info(f'Syncing page with prev={last_gid}')
        new_gid = sync_page(last_gid, expunged=False)
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
