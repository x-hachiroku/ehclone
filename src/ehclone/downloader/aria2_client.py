import time
import requests

from ehclone.config import config
from ehclone.logger import logger


class Aria2Client:
    def __init__(self):
        self.url = config.aria2.url
        self.token = config.aria2.token
        self.base_dir = str(config.aria2.remote_dir)
        self.base_params = []
        if self.token:
            self.base_params.append(f'token:{self.token}')

    def add_task(self, url, out, prioritize=False):
        options = {
            'out': str(out),
            'dir': self.base_dir,
        }

        params = [
            [url],
            options
        ]

        if prioritize:
            params.append(0)

        rpc_params = {
            'jsonrpc': '2.0',
            'id': 'ehclone',
            'method': 'aria2.addUri',
            'params': self.base_params + params
        }

        try:
            res = requests.post(self.url, json=rpc_params)
            res.raise_for_status()
            result = res.json()
            if 'error' in result:
                logger.error(f'Aria2 error: {result['error']}')
                return None
            else:
                logger.info(f'Added to Aria2: {result['result']}')
                return result['result']

        except Exception as e:
            logger.error(f'Failed to send to Aria2: {e}')
            return None

    def get_status(self, task_id):
        params = [
            task_id,
            [ 'gid', 'status']
        ]

        rpc_params = {
            'jsonrpc': '2.0',
            'id': 'ehclone',
            'method': 'aria2.tellStatus',
            'params': self.base_params + params
        }

        try:
            res = requests.post(self.url, json=rpc_params)
            result = res.json()
            return result

        except Exception as e:
            logger.error(f'Error checking aria2 status: {e}')
            return None

    def wait_for_tasks(self, task_ids, timeout=None):
        remaining = set(task_ids)
        results = {}
        start_time = time.time()

        while remaining:
            for task_id in list(remaining):
                status = self.get_status(task_id)
                if not status:
                    continue

                if 'error' in status:
                    results[task_id] = status
                    remaining.remove(task_id)
                    continue

                result = status.get('result') or {}
                state = result.get('status')

                if state in {'complete', 'error', 'removed'}:
                    results[task_id] = status
                    remaining.remove(task_id)

            if not remaining:
                break

            if timeout is not None and time.time() - start_time > timeout:
                logger.error(f'Timeout waiting for aria2 tasks: {remaining}')
                break

            time.sleep(config.aria2.poll_interval)

        return results, remaining


aria2 = Aria2Client()
