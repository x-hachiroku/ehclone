#!/usr/bin/env python3

from time import sleep
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter

from ehclone.config import config
from ehclone.logger import logger


class EHSession:
    GDATA_LIMIT = 25

    def __init__(self):
        self.session = requests.Session()
        self.session.mount('https://', HTTPAdapter(max_retries=5))
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36',
        })

        if config.eh.cookies:
            requests.utils.add_dict_to_cookiejar(
                self.session.cookies,
                config.eh.cookies
            )
        else:
            logger.warning('No cookies configured.')

        if config.eh.proxy:
            url = config.eh.proxy
            logger.info(f'Using proxy: {url}')
            self.session.proxies.update({
                'http': url,
                'https': url,
            })
        else:
            logger.info('No proxies configured.')

        self.last_req_time = 0
        self.base_url = config.eh.base_url.strip('/')

    def _wait(self):
        _wait_sec = config.eh.min_request_interval - (datetime.now().timestamp() - self.last_req_time)
        if _wait_sec > 0:
            sleep(_wait_sec)
        self.last_req_time = datetime.now().timestamp()

    def _get(self, url, *args, **kwargs):
        self._wait()
        res = self.session.get(url, *args, **kwargs)
        res.raise_for_status()
        return res

    def get(self, path, *args, **kwargs):
        res = self._get(self.base_url + path, *args, **kwargs)
        return res

    def _post(self, url, *args, **kwargs):
        self._wait()
        res = self.session.post(url, *args, **kwargs)
        res.raise_for_status()
        return res

    def post(self, path, *args, **kwargs):
        res = self._post(self.base_url + path, *args, **kwargs)
        return res

    def gdata(self, gidlist):
        giddict = { g[0]: g[1] for g in gidlist }
        prev_len = len(giddict)

        results = []
        while giddict:
            _gidlist = [list(g) for g in list(giddict.items())[:self.GDATA_LIMIT]]

            _json = {
                'method': 'gdata',
                'gidlist': _gidlist,
                'namespace': 1,
            }

            try:
                res = self.post('/api.php', json=_json)
            except requests.RequestException as e:
                logger.error(f'GDATA post failed: {e}')
                break

            gmetadata = res.json().get('gmetadata', [])
            if not isinstance(gmetadata, list):
                gmetadata = []

            for d in gmetadata:
                gid = d.get('gid')
                token = d.get('token')
                if gid and token and token == giddict.get(gid):
                    results.append(d)
                    del giddict[gid]
                else:
                    logger.error(f'GDATA response missing or invalid for gid: {gid}')

            if len(giddict) == prev_len:
                break
            prev_len = len(giddict)

        if giddict:
            logger.error(f'Failed to retrieve metadata for gids: {list(giddict)}')
            return {}

        return results


ehs = EHSession()
