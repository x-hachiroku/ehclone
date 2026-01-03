#!/usr/bin/env python3

from urllib.parse import urlencode
from bs4 import BeautifulSoup

from ehclone.config import config
from ehclone.logger import logger
from ehclone.core.eh_session import ehs
from ehclone.db.crud.gallery import insert_galleries, get_last_gid


def sync_page(prev, expunged=False):
    '''
    Get a page of galleries from EH
    :param prev: The last gallery ID from the previous page
    :param expunged: Whether to search for expunged galleries

    :return: last gallery ID, -1 if no galleries found
    '''

    _args = config.eh.get_search_args()
    _args['prev'] = prev

    if expunged:
        _args['f_sh'] = 'on'
    elif 'f_sh' in _args:
        del _args['f_sh']

    _query = urlencode(_args)

    try:
        res = ehs.get('/?' + _query)
        soup = BeautifulSoup(res.text, 'lxml')
        galleries_a = soup.select('table.itg > tr > td.gl3c > a')
    except Exception as e:
        logger.error(f'Error fetching galleries: {e}')
        return -1

    gidlist = []
    for a in galleries_a:
        href = a['href'].strip('/')
        gid, token = href.split('/')[-2:]
        gidlist.append([int(gid), token])
    logger.debug(f'Found {len(gidlist)} galleries: {list(map(lambda x: f"{x[0]}/{x[1]}", gidlist))}')

    if not gidlist:
        return -1

    _gdata = ehs.gdata(gidlist)
    logger.debug(f'Retrieved metadata for {len(_gdata)} galleries: {list(map(lambda x: f"{x["gid"]}/{x["token"]}", _gdata))}')

    insert_galleries(_gdata)

    return gidlist[0][0]


def sync_index():
    last_gid = get_last_gid(expunged=False, categories=config.eh.categories)
    logger.info(f'Starting sync from {last_gid}')

    while True:
        logger.info(f'Syncing page with prev={last_gid}')
        new_gid = sync_page(last_gid, expunged=False)
        if new_gid == -1:
            logger.info('Sync complete, no more galleries found.')
            break

        last_gid = new_gid

    if config.eh.include_expunged:
        last_gid = get_last_gid(expunged=True, categories=config.eh.categories)
        logger.info(f'Starting expunged sync from {last_gid}')

        while True:
            logger.info(f'Syncing expunged page with prev={last_gid}')
            new_gid = sync_page(last_gid, expunged=True)
            if new_gid == -1:
                logger.info('Expunged sync complete, no more galleries found.')
                break

            last_gid = new_gid
