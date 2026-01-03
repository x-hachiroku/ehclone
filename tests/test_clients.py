import json
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from ehclone.downloader.qbit import QbitClient
from ehclone.downloader.aria2 import Aria2Client
from ehclone.downloader import qbit, aria2


@pytest.mark.parametrize('torrent_key,cookies,tracker', [
    (None, {}, 'https://ehtracker.org/123/announce'),
    ('', {'ipb_member_id': '456'}, 'https://ehtracker.org/123/announce'),
    ('secret', {'ipb_member_id': '456'}, 'https://ehtracker.org/123/456xsecret/announce'),
])
def test_qbit_submission_includes_gallery_tracker(monkeypatch, torrent_key, cookies, tracker):
    api = Mock()
    api.torrents_info.return_value = []
    api.torrents_add.return_value = 'Ok.'
    monkeypatch.setattr(qbit.qbittorrentapi, 'Client', Mock(return_value=api))
    monkeypatch.setattr(qbit.config.eh, 'torrent_key', torrent_key)
    monkeypatch.setattr(qbit.config.eh, 'cookies', cookies)

    assert QbitClient().add_torrent(123, 'token', 'ABC') == 'abc'

    magnet = urlsplit(api.torrents_add.call_args.kwargs['urls'])
    assert magnet.scheme == 'magnet'
    assert parse_qs(magnet.query) == {'xt': ['urn:btih:ABC'], 'tr': [tracker]}


def test_qbit_adapter_paths_and_hashes(monkeypatch):
    api = Mock()
    api.torrents_info.return_value = []
    api.torrents_add.return_value = 'Ok.'
    monkeypatch.setattr(qbit.qbittorrentapi, 'Client', Mock(return_value=api))
    client = QbitClient()
    assert client.add_torrent(1, 'token', 'ABC') == 'abc'
    assert api.torrents_add.call_args.kwargs['save_path'] == '/remote/qbit/1_token'
    client.get_torrents_info(['abc'])
    api.torrents_info.assert_called_with(torrent_hashes=['abc'])
    client.delete_torrents(['abc'])
    api.torrents_delete.assert_called_once_with(torrent_hashes=['abc'], delete_files=True)
    api.torrents_add.return_value = 'Fails.'
    assert client.add_torrent(1, 'token', 'ABC') is None
    # Newer Web API versions return metadata instead of the string "Ok.".
    api.torrents_add.return_value = {'added': ['abc']}
    assert client.add_torrent(1, 'token', 'ABC') == 'abc'
    api.torrents_info.side_effect = ConnectionError('offline')
    with pytest.raises(ConnectionError):
        client.get_torrents_info(['abc'])


@pytest.mark.parametrize('token', [None, '', 'rpc-secret'])
def test_aria2_submission_and_status(monkeypatch, token):
    monkeypatch.setattr(aria2.config.aria2, 'token', token)
    auth = [f'token:{token}'] if token else []
    response = Mock()
    response.json.return_value = {'result': 'ec00000000000123'}
    post = Mock(return_value=response)
    monkeypatch.setattr(aria2.requests, 'post', post)
    client = Aria2Client()
    assert client.add_task(123, 'https://archive', '123_token.cbz') == 'ec00000000000123'
    options = {
        'gid': 'ec00000000000123', 'dir': '/remote/aria2', 'out': '123_token.cbz',
        'auto-file-renaming': 'false', 'allow-overwrite': 'true',
    }
    post.assert_called_once_with('http://aria2/jsonrpc', json={
        'jsonrpc': '2.0', 'id': 'ehclone', 'method': 'aria2.addUri',
        'params': auth + [['https://archive'], options],
    }, timeout=30)
    response.json.return_value = {'result': {'gid': 'ec00000000000123', 'status': 'active'}}
    assert client.get_status(123)['status'] == 'active'
    assert post.call_args.kwargs['json'] == {
        'jsonrpc': '2.0', 'id': 'ehclone', 'method': 'aria2.tellStatus',
        'params': auth + ['ec00000000000123', ['gid', 'status']],
    }
    client.remove_result(123)
    assert post.call_args.kwargs['json'] == {
        'jsonrpc': '2.0', 'id': 'ehclone', 'method': 'aria2.removeDownloadResult',
        'params': auth + ['ec00000000000123'],
    }
    post.side_effect = ConnectionError('offline')
    with pytest.raises(ConnectionError):
        client.get_status(123)


@pytest.mark.parametrize('result', [None, '', 'unexpected-id'])
def test_aria2_rejects_unexpected_submission_id(monkeypatch, result):
    client = Aria2Client()
    monkeypatch.setattr(client, '_rpc', Mock(return_value=result))
    with pytest.raises(RuntimeError, match='requested task ID'):
        client.add_task(123, 'https://archive', '123_token.cbz')


@pytest.mark.parametrize('path,expected', [('/remote/qbit/1_token', 'abc'), ('/other', None)])
def test_existing_torrent_is_reused_only_in_its_managed_directory(monkeypatch, path, expected):
    from types import SimpleNamespace

    api = Mock()
    api.torrents_info.return_value = [SimpleNamespace(save_path=path)]
    monkeypatch.setattr(qbit.qbittorrentapi, 'Client', Mock(return_value=api))
    client = QbitClient()
    assert client.add_torrent(1, 'token', 'abc') == expected
    api.torrents_add.assert_not_called()
    api.torrents_delete.assert_not_called()


@pytest.mark.parametrize('status_code', [200, 400])
@pytest.mark.parametrize('method', ['get_status', 'remove_result'])
def test_aria2_distinguishes_missing_task_from_rpc_failure(monkeypatch, status_code, method):
    response = requests.Response()
    response.status_code = status_code
    monkeypatch.setattr(aria2.requests, 'post', Mock(return_value=response))
    client = Aria2Client()
    response._content = json.dumps({
        'id': 'ehclone', 'jsonrpc': '2.0',
        'error': {'code': 1, 'message': 'GID ec00000000000123 is not found'},
    }).encode()
    assert getattr(client, method)(123) is None
    response._content = b'{"error": {"code": 1, "message": "Unauthorized"}}'
    with pytest.raises(aria2.Aria2Error, match='Unauthorized'):
        getattr(client, method)(123)


@pytest.mark.parametrize('body', [b'<html>Bad Gateway</html>', b'{"message": "Bad Gateway"}'])
def test_aria2_preserves_http_errors_without_rpc_errors(monkeypatch, body):
    response = requests.Response()
    response.status_code = 502
    response._content = body
    monkeypatch.setattr(aria2.requests, 'post', Mock(return_value=response))
    with pytest.raises(requests.HTTPError):
        Aria2Client().get_status(123)


def test_aria2_rejects_invalid_json_in_success_response(monkeypatch):
    response = requests.Response()
    response.status_code = 200
    response._content = b'not JSON'
    monkeypatch.setattr(aria2.requests, 'post', Mock(return_value=response))
    with pytest.raises(requests.exceptions.JSONDecodeError):
        Aria2Client().get_status(123)


def test_aria2_result_cleanup_retries_service_errors_but_accepts_missing_results(monkeypatch):
    client = Aria2Client()
    rpc = Mock(side_effect=ConnectionError('offline'))
    monkeypatch.setattr(client, '_rpc', rpc)
    with pytest.raises(ConnectionError):
        client.remove_result(123)
    rpc.side_effect = aria2.Aria2Error({'code': 1, 'message': 'Unauthorized'})
    with pytest.raises(aria2.Aria2Error, match='Unauthorized'):
        client.remove_result(123)
    rpc.side_effect = aria2.Aria2Error({'code': 1, 'message': 'GID ec00000000000123 is not found.'})
    client.remove_result(123)
