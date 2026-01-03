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

    _args = config.eh.search_args.copy()
    _args['prev'] = prev

    if expunged:
        _args['f_sh'] = 'on'
    elif 'f_sh' in _args:
        del _args['f_sh']

    _query = urlencode(_args)

    try:
        res = ehs.get('/?' + _query)
        res.raise_for_status()
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

    if not gidlist:
        return -1

    _gdata = ehs.gdata(gidlist)

    insert_galleries(_gdata)

    ## TODO
    if gidlist[0][0] > 2000:
        return -1

    return gidlist[0][0]


def sync_index():
    last_gid = get_last_gid(expunged=False)
    logger.info(f'Starting sync from {last_gid}')

    while True:
        logger.info(f'Syncing page with prev={last_gid}')
        new_gid = sync_page(last_gid, expunged=False)
        if new_gid == -1:
            logger.info('Sync complete, no more galleries found.')
            break

        last_gid = new_gid

    if config.eh.include_expunged:
        last_gid = get_last_gid(expunged=True)
        logger.info(f'Starting expunged sync from {last_gid}')

        while True:
            logger.info(f'Syncing expunged page with prev={last_gid}')
            new_gid = sync_page(last_gid, expunged=True)
            if new_gid == -1:
                logger.info('Expunged sync complete, no more galleries found.')
                break

            last_gid = new_gid
