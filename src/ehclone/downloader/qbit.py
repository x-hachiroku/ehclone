from pathlib import Path
from urllib.parse import quote

import qbittorrentapi

from ehclone.config import config


class QbitClient:
    TRACKER = f'https://ehtracker.org'

    def __init__(self):
        self.client = qbittorrentapi.Client(
            host=config.qbit.url,
            username=config.qbit.username,
            password=config.qbit.password,
            REQUESTS_ARGS={'timeout': 30},
        )
        self.client.auth_log_in()

    def add_torrent(self, gid: int, token: str, info_hash: str) -> str | None:
        save_path = config.qbit.remote_dir / f'{gid}_{token}'
        # A retry may find a torrent accepted before an RPC connection was lost.
        # Never adopt (or later delete) a torrent belonging to another directory.
        existing = self.get_torrents_info([info_hash])
        if existing:
            return info_hash.lower() if self.matches_directory(existing[0], gid, token) else None
        tracker = QbitClient.TRACKER + f'/{gid}'
        if config.eh.torrent_key:
            tracker += f'/{config.eh.cookies["ipb_member_id"]}x{config.eh.torrent_key}'
        tracker += '/announce'
        result = self.client.torrents_add(
            urls=f'magnet:?xt=urn:btih:{info_hash}&tr={quote(tracker, safe="")}',
            save_path=str(save_path),
            category=config.qbit.category or '',
            use_auto_torrent_management=False,
            is_paused=False,
            is_stopped=False,
        )
        if result == 'Fails.':
            return None
        return info_hash.lower()

    @staticmethod
    def matches_directory(torrent, gid: int, token: str) -> bool:
        return Path(torrent.save_path) == config.qbit.remote_dir / f'{gid}_{token}'

    def get_torrents_info(self, hashes):
        # Let connection failures propagate; they do not mean a torrent disappeared.
        return self.client.torrents_info(torrent_hashes=hashes)

    def delete_torrents(self, hashes, delete_files=True):
        self.client.torrents_delete(torrent_hashes=hashes, delete_files=delete_files)
