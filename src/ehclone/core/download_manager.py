from datetime import datetime, timezone

from ehclone.config import config
from ehclone.core.session import ehs
from ehclone.core.validator import validate
from ehclone.db.entities import DownloadState as State, Gallery, Tag
from ehclone.db.session import session_generator
from ehclone.db.crud.downloads import (
    active_counts,
    active_gallery_ids,
    iter_queued_gallery_ids
)
from ehclone.downloader.aria2 import Aria2Client, task_id as archive_task_id
from ehclone.downloader.qbit import QbitClient
from ehclone.logger import gallery_logger, logger


FAILED = (State.DOWNLOAD_FAILED, State.VALIDATION_FAILED)


class DownloadManager:
    """Check persisted tasks, fill each backend's slots, then return."""

    def run(self) -> None:
        self.aria2 = Aria2Client() if config.aria2.url else None
        self.qbit = None
        if config.qbit.enabled:
            try:
                self.qbit = QbitClient()
            except Exception:
                logger.exception('Cannot connect to qBittorrent; retrying next run')

        with session_generator() as session:
            active_ids = active_gallery_ids(session)
        logger.debug('Fetched {} galleries with active downloads or pending cleanup', len(active_ids))
        for gid in active_ids:
            log = gallery_logger(gid, None)
            try:
                with session_generator() as session:
                    gallery = session.get(Gallery, gid)
                    log = gallery_logger(gid, gallery.token)
                    with logger.contextualize(gid=gid, token=gallery.token):
                        self._check(gallery)
                        session.commit()
                        self._cleanup_archive(gallery)
            except Exception:
                log.exception('Failed to check gallery')

        with session_generator() as session:
            qbit_active, aria2_active = active_counts(session)
        if not self._can_enqueue(qbit_active, aria2_active):
            return
        for gid in iter_queued_gallery_ids():
            log = gallery_logger(gid, None)
            try:
                with session_generator() as session:
                    gallery = session.get(Gallery, gid)
                    log = gallery_logger(gid, gallery.token)
                    with logger.contextualize(gid=gid, token=gallery.token):
                        self._enqueue(gallery, qbit_active, aria2_active)
                    torrent_added = gallery.bittorrent == State.DOWNLOADING
                    archive_added = gallery.archiver == State.DOWNLOADING
                # Only committed transitions consume a slot.
                qbit_active += torrent_added
                aria2_active += archive_added
            except Exception:
                log.exception('Failed to enqueue gallery')
            if not self._can_enqueue(qbit_active, aria2_active):
                break

    def _can_enqueue(self, qbit_active: int, aria2_active: int) -> bool:
        return bool(
            (self.qbit and self._has_slot(qbit_active, config.qbit.task_limit))
            or (self.aria2 and self._has_slot(aria2_active, config.aria2.task_limit))
        )

    @staticmethod
    def _has_slot(active: int, limit: int) -> bool:
        return limit < 0 or active < limit

    def _enqueue(self, gallery: Gallery, qbit_active: int, aria2_active: int) -> None:
        if gallery.expunged and not config.eh.include_expunged:
            return
        if config.filter.dedupe.enabled and (
            gallery.updated_at is None or gallery.deduped_at is None
            or gallery.deduped_at < gallery.updated_at
        ):
            return
        if gallery.rating is None or gallery.rating * 0.01 < config.filter.min_rating:
            return
        if config.filter.include_tags:
            tags = {(tag.namespace, tag.name) for tag in gallery.tags}
            for tag_string in config.filter.include_tags:
                tag = Tag.from_str(tag_string)
                if (tag.namespace, tag.name) not in tags:
                    return
        if gallery.bittorrent == State.QUEUED:
            if not config.qbit.enabled or not gallery.torrents:
                gallery.bittorrent = State.DOWNLOAD_FAILED
            elif self.qbit and self._has_slot(qbit_active, config.qbit.task_limit):
                torrent = max(gallery.torrents, key=lambda t: (
                    t.added_at or datetime.min, t.infohash
                ))
                try:
                    task_id = self.qbit.add_torrent(gallery.gid, gallery.token, torrent.infohash)
                except Exception:
                    # Stop new submissions to an unavailable service this run.
                    self.qbit = None
                    raise
                if task_id:
                    gallery.bittorrent_task_id = task_id
                    gallery.bittorrent_started_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    gallery.bittorrent = State.DOWNLOADING
                else:
                    gallery.bittorrent = State.DOWNLOAD_FAILED

        if (gallery.bittorrent in FAILED and self.aria2
                and self._has_slot(aria2_active, config.aria2.task_limit)):
            self._start_archive(gallery)

    def _start_archive(self, gallery: Gallery) -> None:
        try:
            status = self.aria2.get_status(gallery.gid)
        except Exception:
            self.aria2 = None
            raise
        if status is None:
            url = ehs.archive_url(gallery.gid, gallery.token)
            try:
                self.aria2.add_task(gallery.gid, url, f'{gallery.gid}_{gallery.token}.cbz')
            except Exception:
                self.aria2 = None
                raise
        gallery.archiver_task_id = archive_task_id(gallery.gid)
        gallery.archiver = State.DOWNLOADING

    def _check(self, gallery: Gallery) -> None:
        if gallery.bittorrent == State.DOWNLOADING and self.qbit:
            self._check_torrent(gallery)
        if (gallery.archiver == State.DOWNLOADING
                and gallery.archiver_task_id and self.aria2):
            try:
                status = self.aria2.get_status(gallery.gid)
            except Exception:
                self.aria2 = None
                raise
            if status is None or status['status'] in {'error', 'removed'}:
                gallery.archiver = State.DOWNLOAD_FAILED
            elif status['status'] == 'complete':
                source = config.aria2.local_dir / f'{gallery.gid}_{gallery.token}.cbz'
                gallery.archiver = (
                    State.COMPLETED if validate(gallery.gid, gallery.token, source)
                    else State.VALIDATION_FAILED
                )

    def _cleanup_archive(self, gallery: Gallery) -> None:
        if (gallery.archiver in {State.COMPLETED, *FAILED}
                and gallery.archiver_task_id and self.aria2):
            if gallery.archiver == State.COMPLETED:
                source = config.aria2.local_dir / f'{gallery.gid}_{gallery.token}.cbz'
                source.unlink(missing_ok=True)
            try:
                self.aria2.remove_result(gallery.gid)
            except Exception:
                self.aria2 = None
                raise
            gallery.archiver_task_id = None

    def _check_torrent(self, gallery: Gallery) -> None:
        try:
            torrents = self.qbit.get_torrents_info([gallery.bittorrent_task_id])
        except Exception:
            self.qbit = None
            raise
        torrent = next((t for t in torrents
                        if t.hash.lower() == gallery.bittorrent_task_id.lower()), None)
        if torrent and not self.qbit.matches_directory(torrent, gallery.gid, gallery.token):
            gallery_logger(gallery.gid, gallery.token).warning(
                'Torrent {} moved outside its managed directory, skipping...',
                gallery.bittorrent_task_id,
            )
            gallery.bittorrent = State.DOWNLOAD_FAILED
            return
        if torrent and torrent.progress >= 1:
            source = config.qbit.local_dir / f'{gallery.gid}_{gallery.token}'
            if validate(gallery.gid, gallery.token, source):
                gallery.bittorrent = State.COMPLETED
            else:
                self.qbit.delete_torrents([gallery.bittorrent_task_id], delete_files=True)
                gallery.bittorrent = State.VALIDATION_FAILED
            return

        started = gallery.bittorrent_started_at
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        expired = (started is not None and (now - started).total_seconds()
                   >= config.qbit.timeout_hours * 3600)
        if torrent is None or torrent.state in {'error', 'missingFiles'} or expired:
            self.qbit.delete_torrents([gallery.bittorrent_task_id], delete_files=True)
            gallery.bittorrent = State.DOWNLOAD_FAILED


def run() -> None:
    DownloadManager().run()


if __name__ == '__main__':
    run()
