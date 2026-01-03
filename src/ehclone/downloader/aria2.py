import re

import requests

from ehclone.config import config


def task_id(gid: int) -> str:
    """Use the decimal gallery ID in a fixed-width, hexadecimal aria2 GID."""
    if not 0 < gid < 10 ** 14:
        raise ValueError('Gallery ID must fit in 14 decimal digits')
    return f'ec{gid:014d}'


class Aria2Error(RuntimeError):
    """A rejected RPC request, distinct from a failed download."""

    def __init__(self, error: dict):
        self.code = error.get('code')
        super().__init__(error.get('message', 'Unknown aria2 error'))


class Aria2Client:
    def __init__(self):
        self.url = config.aria2.url
        self.base_dir = str(config.aria2.remote_dir)
        self.base_params = [f'token:{config.aria2.token}'] if config.aria2.token else []

    def _rpc(self, method: str, params: list):
        response = requests.post(self.url, json={
            'jsonrpc': '2.0',
            'id': 'ehclone',
            'method': f'aria2.{method}',
            'params': self.base_params + params,
        }, timeout=30)
        try:
            payload = response.json()
        except requests.exceptions.JSONDecodeError:
            response.raise_for_status()
            raise
        # aria2 sends RPC failures (including missing GIDs) with HTTP 400.
        if isinstance(payload, dict) and 'error' in payload:
            raise Aria2Error(payload['error'])
        response.raise_for_status()
        return payload['result']

    def add_task(self, gid: int, url: str, out: str) -> str:
        archive_id = task_id(gid)
        options = {
            'gid': archive_id,
            'out': str(out),
            'dir': self.base_dir,
            'auto-file-renaming': 'false',
            'allow-overwrite': 'true',
        }
        params = [[url], options]
        result = self._rpc('addUri', params)
        if result != archive_id:
            raise RuntimeError('aria2 did not return the requested task ID')
        return archive_id

    def get_status(self, gid: int) -> dict | None:
        """Return None only for a missing task; service errors propagate."""
        try:
            return self._rpc('tellStatus', [task_id(gid), ['gid', 'status']])
        except Aria2Error as exc:
            if exc.code == 1 and re.fullmatch(r'GID .+ is not found', str(exc).rstrip('.')):
                return None
            raise

    def remove_result(self, gid: int) -> None:
        try:
            self._rpc('removeDownloadResult', [task_id(gid)])
        except Aria2Error as exc:
            # Cleanup retries can find a result removed before a DB commit.
            if exc.code == 1 and re.fullmatch(r'GID .+ is not found', str(exc).rstrip('.')):
                return
            raise
