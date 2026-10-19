"""Download thumbnails and compute their stored vectors."""

from pathlib import Path
from urllib.parse import urlsplit

from requests import HTTPError

from ehclone.config import config
from ehclone.core.session import ehs
from ehclone.db.crud.thumb import (
    iter_unvectorized_thumb_batches,
    mark_thumb_not_found,
    update_thumb_vector,
)
from ehclone.logger import logger
from ehclone.vectorizer.mobile_net_v3 import get_vectorizer


DOWNLOAD_TIMEOUT = 60


def thumbnail_path(url: str) -> Path:
    path = Path(urlsplit(url).path.strip('/'))
    if not path.resolve().is_relative_to(Path('.').resolve()):
        raise ValueError(f'Suspicious path, check path traversal: {url}')
    suffix = path.suffix.lower()
    if suffix not in {'.jpg', '.jpeg', '.png', '.webp', '.gif'}:
        suffix = '.webp'
    return path.with_suffix(suffix)


def download_thumb(url: str | None) -> bytes | None:
    if not url:
        return
    relative_path = thumbnail_path(url)

    # CDN thumbnails do not consume the gallery/API request interval.
    response = ehs.get(url, skip_wait=True, timeout=DOWNLOAD_TIMEOUT)
    if not response.headers.get('Content-Type', '').lower().startswith('image/'):
        raise ValueError('Response is not an image')
    content = response.content
    if thumb_dir := config.filter.dedupe.thumb_dir:
        thumb_path = thumb_dir / relative_path
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        thumb_path.write_bytes(content)
    return content


def update_encode(url, path) -> bool:
    if vector := get_vectorizer().encode(path):
        update_thumb_vector(url, vector)
        return True
    return False


def run() -> None:
    for thumb_urls in iter_unvectorized_thumb_batches():
        successes = 0
        for url in thumb_urls:
            try:
                try:
                    image_bytes = download_thumb(url)
                except HTTPError as exc:
                    if exc.response is None or exc.response.status_code != 404:
                        raise
                    mark_thumb_not_found(url)
                    logger.warning('Thumbnail not found; skipping future work for {}', url)
                    continue
                if update_encode(url, image_bytes):
                    successes += 1
            except Exception:
                logger.exception('Failed to process thumbnail {}', url)
                continue
        logger.info('{}/{} thumbnail vectors computed', successes, len(thumb_urls))


if __name__ == '__main__':
    run()
