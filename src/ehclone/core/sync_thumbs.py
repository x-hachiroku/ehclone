import shutil
from pathlib import Path
from urllib.parse import urlparse

from ehclone.config import config
from ehclone.logger import logger
from ehclone.downloader.aria2_client import aria2
from ehclone.db.crud.thumb import get_unvectorized_thumbs, update_thumb_vector
from ehclone.vectorizer.mobile_net_v3 import vectorizer


def sync_thumbs():
    while True:
        thumb_urls = get_unvectorized_thumbs(limit=config.aria2.task_limit * 2)

        if not thumb_urls:
            logger.info('All thumbs processed.')
            break
        logger.info(f'Downloading {len(thumb_urls)} thumbs')

        tasks = {}
        for url in thumb_urls:
            parsed = urlparse(url)
            out_subpath = Path('thumb') / 'incomplete' / parsed.path.lstrip('/')

            task_id = aria2.add_task(url=url, out=out_subpath, prioritize=True)
            if task_id:
                tasks[task_id] = (url, out_subpath)
            else:
                logger.error(f'Failed to add aria2 task for {url}')

        logger.info(f'Waiting for {len(tasks)} downloads to complete...')
        results, remaining = aria2.wait_for_tasks(list(tasks.keys()))

        if remaining:
            logger.warning(f'{len(remaining)} tasks did not complete')

        success_count = 0
        for task_id, (url, out_subpath) in tasks.items():
            result = results.get(task_id, {})
            status = result.get('result', {}).get('status')
            if status != 'complete':
                logger.error(f'Download failed for {url}: {result}')
                continue

            local_path = config.aria2.local_dir / out_subpath

            if local_path.exists():
                vector = vectorizer.encode(local_path)
                if vector:
                    update_thumb_vector(url, vector)
                    success_count += 1

                thumb_dir = config.filter.dedupe.thumb_dir

                # Cleanup
                if thumb_dir is None:
                    try:
                        local_path.unlink()
                        logger.debug(f'Deleted processed thumb: {local_path}')
                    except Exception as e:
                        logger.warning(f'Failed to delete {local_path}: {e}')

                else:
                    target_path = thumb_dir / out_subpath
                    try:
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(local_path), str(target_path))
                        logger.debug(f'Moved thumb to {target_path}')
                    except Exception as e:
                        logger.warning(f'Failed to move {local_path} to {target_path}: {e}')

            else:
                logger.error(f'File not found after download: {local_path}')

        logger.info(f'{success_count}/{len(tasks)} vectors computed')
