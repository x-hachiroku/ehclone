from unittest.mock import Mock

import pytest
import requests

from ehclone.core import sync_thumbs
from ehclone.core.session import EHSession


def test_thumbnail_download_skips_spacing_without_forwarding_option(monkeypatch, tmp_path):
    client = EHSession()
    wait = Mock()
    monkeypatch.setattr(client, '_wait', wait)
    response = Mock(headers={'Content-Type': 'image/jpeg'}, content=b'thumbnail')
    get = Mock(return_value=response)
    monkeypatch.setattr(client.session, 'get', get)
    monkeypatch.setattr(sync_thumbs, 'ehs', client)
    monkeypatch.setattr(sync_thumbs.config.filter.dedupe, 'thumb_dir', tmp_path)
    monkeypatch.setattr(sync_thumbs, 'DOWNLOAD_TIMEOUT', 17)

    assert sync_thumbs.download_thumb('https://thumbs.example/image.jpg') == b'thumbnail'
    get.assert_called_once_with('https://thumbs.example/image.jpg', timeout=17)
    wait.assert_not_called()
    assert (tmp_path / 'image.jpg').read_bytes() == b'thumbnail'


def test_all_thumbnails_in_batch_are_attempted_despite_failures(monkeypatch):
    monkeypatch.setattr(sync_thumbs, 'iter_unvectorized_thumb_batches',
                        lambda: iter([['first', 'broken'], ['last']]))
    download = Mock(side_effect=[b'first', ConnectionError('offline'), b'last'])
    monkeypatch.setattr(sync_thumbs, 'download_thumb', download)
    encode = Mock(return_value=True)
    monkeypatch.setattr(sync_thumbs, 'update_encode', encode)
    sync_thumbs.run()
    assert [call.args[0] for call in download.call_args_list] == ['first', 'broken', 'last']
    assert [call.args for call in encode.call_args_list] == [('first', b'first'), ('last', b'last')]


@pytest.mark.parametrize('status', [404, 403, 500])
def test_only_final_download_404_is_recorded_after_session_retries(monkeypatch, status):
    client = EHSession()
    monkeypatch.setattr(sync_thumbs.config.eh, 'max_retry', 2)
    monkeypatch.setattr(sync_thumbs.config.filter.dedupe, 'thumb_dir', None)
    monkeypatch.setattr(sync_thumbs, 'ehs', client)
    failed = requests.Response()
    failed.status_code = status
    failed._content = b''
    failed._content_consumed = True
    success = requests.Response()
    success.status_code = 200
    success.headers['Content-Type'] = 'image/jpeg'
    success._content = b'image'
    get = Mock(side_effect=[failed, failed, failed, success])
    monkeypatch.setattr(client.session, 'get', get)
    urls = ['https://thumbs.example/missing.jpg', 'https://thumbs.example/good.jpg']
    monkeypatch.setattr(sync_thumbs, 'iter_unvectorized_thumb_batches', lambda: iter([urls]))

    def record(url):
        assert get.call_count == 3
        assert url == urls[0]

    mark = Mock(side_effect=record)
    monkeypatch.setattr(sync_thumbs, 'mark_thumb_not_found', mark)
    encode = Mock(return_value=True)
    monkeypatch.setattr(sync_thumbs, 'update_encode', encode)

    sync_thumbs.run()

    assert get.call_count == 4
    assert mark.call_count == (1 if status == 404 else 0)
    encode.assert_called_once_with(urls[1], b'image')
