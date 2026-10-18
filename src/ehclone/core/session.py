import re
from datetime import timezone
from email.utils import parsedate_to_datetime
from html import unescape
from time import monotonic, sleep, time
from urllib.parse import urljoin, urlsplit

import requests

from ehclone.config import config
from ehclone.logger import gallery_logger, logger


class EHSession:
    GDATA_LIMIT = 25
    DEFAULT_TIMEOUT = 60
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36'

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': self.USER_AGENT})

        if config.eh.cookies:
            hostname = urlsplit(config.eh.base_url).hostname
            for name, value in config.eh.cookies.items():
                self.session.cookies.set(name, value, domain=hostname, path='/')
        else:
            logger.warning('No cookies configured.')

        if config.eh.proxy:
            logger.info(f'Enabled proxy: {config.eh.proxy}')
            self.session.proxies.update({
                'http': config.eh.proxy,
                'https': config.eh.proxy,
            })
        else:
            logger.info('No proxies configured.')

        self.last_req_time = 0
        self.base_url = config.eh.base_url

    def _wait(self):
        wait_sec = config.eh.min_request_interval - (monotonic() - self.last_req_time)
        if wait_sec > 0:
            sleep(wait_sec)
        self.last_req_time = monotonic()

    def _retry_delay(self, response: requests.Response | None) -> float:
        retry_after = response.headers.get('Retry-After') if response is not None else None
        if retry_after:
            retry_after = retry_after.strip()
            if re.fullmatch(r'[0-9]+', retry_after):
                return int(retry_after)
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                retry_after = max(0, retry_at.timestamp() - time())
                return min(retry_after, 100 * config.eh.min_request_interval)
            except (ValueError, TypeError, OverflowError):
                pass
        return 10 * config.eh.min_request_interval

    def _request(self, method: str, url: str, *args, skip_wait=False, **kwargs) -> requests.Response:
        kwargs.setdefault('timeout', self.DEFAULT_TIMEOUT)
        # Count only errors other than 429/503 against the retry budget.
        logger.debug(f'{method}: {url}')
        retries = 0
        while True:
            if not skip_wait:
                self._wait()
            response = None
            try:
                response = getattr(self.session, method)(url, *args, **kwargs)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                if response is None:
                    response = exc.response
                if response is not None:
                    response.close()
                if not (response is not None and response.status_code in (429, 503)):
                    retries += 1
                    if retries > config.eh.max_retry:
                        raise
                delay = self._retry_delay(response)
                logger.warning(f'{method.upper()} {url} failed: {exc}; retrying in {delay}s')
                sleep(delay)

    def _get(self, url, *args, **kwargs):
        return self._request('get', url, *args, **kwargs)

    def get(self, path, *args, **kwargs):
        url = urljoin(self.base_url, path)
        target = urlsplit(url)
        base = urlsplit(self.base_url)
        if (target.scheme, target.netloc) != (base.scheme, base.netloc):
            return self._get(url, *args, **kwargs)

        while True:
            response = self._get(url, *args, **kwargs)
            # EH returns 200 with with content-type text/html and a plaintext for too many requests...
            if re.search(r'<!doctype\s+html\b|<html\b', response.text, re.IGNORECASE):
                return response
            delay = self._retry_delay(response)
            logger.warning(f'GET {url} returned non-HTML content. Retrying in {delay}s')
            sleep(delay)

    def _post(self, url, *args, **kwargs):
        return self._request('post', url, *args, **kwargs)

    def post(self, path, *args, **kwargs):
        return self._post(urljoin(self.base_url, path), *args, **kwargs)

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

            try:
                data = res.json()
            except requests.exceptions.JSONDecodeError:
                delay = self._retry_delay(res)
                logger.warning(f'GDATA returned non-JSON content. Retrying in {delay}s')
                sleep(delay)
                continue

            gmetadata = data.get('gmetadata', [])
            if not isinstance(gmetadata, list):
                gmetadata = []

            for d in gmetadata:
                gid = d.get('gid')
                token = d.get('token')
                if gid and token and token == giddict.get(gid):
                    results.append(d)
                    del giddict[gid]
                else:
                    gallery_logger(gid, token).error('GDATA response missing or invalid')

            if len(giddict) == prev_len:
                break
            prev_len = len(giddict)

        if giddict:
            for gid, token in giddict.items():
                gallery_logger(gid, token).error('Failed to retrieve metadata')
            return []

        return results

    def archive_url(self, gid: int, token: str) -> str:
        form_url = f'/archiver.php?gid={gid}&token={token}'
        response = self.post(form_url, data={
            'dltype': 'org',
            'dlcheck': 'Download Original Archive',
        })
        match = re.search(r"""document\.location\s*=\s*["']([^"']+)["']""", response.text)
        if match is None:
            raise ValueError(f'No original archive download URL for gallery {gid}')
        url = urljoin(form_url, unescape(match.group(1)))
        return url + '?start=1'


ehs = EHSession()
