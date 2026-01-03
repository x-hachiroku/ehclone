from types import SimpleNamespace
from unittest.mock import Mock

import requests
import pytest

from ehclone.config import config
from ehclone.core import session

from ehclone.core.session import EHSession


def test_original_archiver_direct_form_and_redirect(monkeypatch):
    client = EHSession()
    get = Mock()
    post = Mock(return_value=SimpleNamespace(text="""
        <script>document.location = "https://download.example/archive";</script>
    """))
    monkeypatch.setattr(client, 'get', get)
    monkeypatch.setattr(client, 'post', post)
    assert client.archive_url(42, 'x') == 'https://download.example/archive?start=1'
    get.assert_not_called()
    post.assert_called_once_with(
        '/archiver.php?gid=42&token=x',
        data={'dltype': 'org', 'dlcheck': 'Download Original Archive'})


def test_archive_request_error_is_propagated(monkeypatch):
    client = EHSession()
    error = requests.ConnectionError('offline')
    post = Mock(side_effect=error)
    monkeypatch.setattr(client, 'post', post)
    with pytest.raises(requests.ConnectionError) as exc:
        client.archive_url(42, 'x')
    assert exc.value is error


def test_no_archive_redirect_is_an_error(monkeypatch):
    client = EHSession()
    monkeypatch.setattr(client, 'post', lambda *args, **kwargs:
                        SimpleNamespace(text='Insufficient funds'))
    with pytest.raises(ValueError, match='No original archive download URL'):
        client.archive_url(42, 'x')


@pytest.mark.parametrize('method', ['get', 'post'])
def test_absolute_urls_are_not_prefixed(monkeypatch, method):
    client = EHSession()
    request = Mock()
    monkeypatch.setattr(client.session, method, request)
    getattr(client, method)('https://download.example/archive')
    assert request.call_args.args[0] == 'https://download.example/archive'


def test_account_cookies_are_not_sent_to_external_thumbnail_hosts(monkeypatch):
    monkeypatch.setattr(config.eh, 'cookies', {'ipb_pass_hash': 'test-cookie'})
    client = EHSession()
    gallery = client.session.prepare_request(requests.Request('GET', config.eh.base_url + '/g/1/a/'))
    thumbnail = client.session.prepare_request(requests.Request('GET', 'https://thumbs.example/image.jpg'))
    assert 'ipb_pass_hash=test-cookie' in gallery.headers['Cookie']
    assert 'Cookie' not in thumbnail.headers


def test_skipping_wait_preserves_spacing_for_normal_requests(monkeypatch):
    client = EHSession()
    monkeypatch.setattr(config.eh, 'min_request_interval', 5)
    now = 100
    monkeypatch.setattr(session, 'monotonic', lambda: now)

    def advance_clock(seconds):
        nonlocal now
        now += seconds

    sleep = Mock(side_effect=advance_clock)
    monkeypatch.setattr(session, 'sleep', sleep)
    get = Mock(return_value=Mock())
    monkeypatch.setattr(client.session, 'get', get)
    client.get('/gallery')
    now = 102
    client.get('https://thumbs.example/skipped.jpg', timeout=60, skip_wait=True)
    get.assert_called_with('https://thumbs.example/skipped.jpg', timeout=60)
    sleep.assert_not_called()
    client.get('https://thumbs.example/image.jpg', timeout=60)
    sleep.assert_called_once_with(3)
    get.assert_called_with('https://thumbs.example/image.jpg', timeout=60)


def test_gdata_request_error_returns_failure(monkeypatch):
    client = EHSession()
    monkeypatch.setattr(client, 'post', Mock(side_effect=requests.ConnectionError('offline')))
    assert client.gdata([[42, 'abc']]) == []


@pytest.mark.parametrize('method', ['get', 'post'])
def test_failed_stream_response_is_closed(monkeypatch, method):
    client = EHSession()
    monkeypatch.setattr(config.eh, 'max_retry', 0)
    response = requests.Response()
    response.status_code = 404
    response.close = Mock()
    monkeypatch.setattr(client.session, method, Mock(return_value=response))
    with pytest.raises(requests.HTTPError) as exc:
        getattr(client, method)('https://thumbs.example/image.jpg', stream=True)
    assert exc.value.response is response
    response.close.assert_called_once()


def make_response(status, retry_after=None):
    response = requests.Response()
    response.status_code = status
    response.close = Mock()
    if retry_after is not None:
        response.headers['Retry-After'] = retry_after
    return response


@pytest.fixture
def retry_client(monkeypatch):
    client = EHSession()
    monkeypatch.setattr(config.eh, 'max_retry', 2)
    monkeypatch.setattr(config.eh, 'min_request_interval', 3)
    monkeypatch.setattr(client, '_wait', Mock())
    monkeypatch.setattr(session, 'sleep', Mock())
    return client


@pytest.mark.parametrize('method', ['get', 'post'])
@pytest.mark.parametrize('status', [429, 503])
def test_busy_responses_retry_until_success(
        monkeypatch, retry_client, method, status):
    monkeypatch.setattr(config.eh, 'max_retry', 0)
    responses = [make_response(status) for _ in range(3)] + [make_response(200)]
    request = Mock(side_effect=responses)
    monkeypatch.setattr(retry_client.session, method, request)

    assert getattr(retry_client, method)(
        '/test', timeout=15, stream=True, skip_wait=True,
    ) is responses[-1]
    assert request.call_count == 4
    retry_client._wait.assert_not_called()
    assert session.sleep.call_count == 3
    for call in request.call_args_list:
        assert call.args == (retry_client.base_url + '/test',)
        assert call.kwargs == {'timeout': 15, 'stream': True}
    for response in responses[:-1]:
        response.close.assert_called_once()
    responses[-1].close.assert_not_called()


@pytest.mark.parametrize('method', ['get', 'post'])
@pytest.mark.parametrize('status', [400, 401, 403, 404, 500, 502, 504])
def test_other_http_errors_stop_after_max_retry(monkeypatch, retry_client, method, status):
    responses = [make_response(status) for _ in range(3)]
    request = Mock(side_effect=responses)
    monkeypatch.setattr(retry_client.session, method, request)

    with pytest.raises(requests.HTTPError) as exc:
        getattr(retry_client, method)('/test')
    assert exc.value.response is responses[-1]
    assert request.call_count == 3
    assert session.sleep.call_count == 2
    session.sleep.assert_called_with(30)
    for response in responses:
        response.close.assert_called_once()


@pytest.mark.parametrize('method', ['get', 'post'])
@pytest.mark.parametrize('error', [requests.ConnectionError, requests.Timeout,
                                  requests.TooManyRedirects,
                                  requests.exceptions.ChunkedEncodingError])
def test_request_exceptions_stop_after_max_retry(monkeypatch, retry_client, method, error):
    failure = error('request failed')
    request = Mock(side_effect=failure)
    monkeypatch.setattr(retry_client.session, method, request)

    with pytest.raises(error) as exc:
        getattr(retry_client, method)('/test')
    assert exc.value is failure
    assert request.call_count == 3
    assert session.sleep.call_count == 2
    session.sleep.assert_called_with(30)


@pytest.mark.parametrize('method', ['get', 'post'])
def test_mixed_errors_share_budget_without_counting_busy_responses(monkeypatch, retry_client, method):
    responses = [make_response(status) for status in [404, 429, 503, 500, 429, 404]]
    request = Mock(side_effect=[requests.ConnectionError('offline'), *responses])
    monkeypatch.setattr(config.eh, 'max_retry', 3)
    monkeypatch.setattr(retry_client.session, method, request)

    with pytest.raises(requests.HTTPError) as exc:
        getattr(retry_client, method)('/test')
    assert exc.value.response is responses[-1]
    assert request.call_count == 7
    assert session.sleep.call_count == 6


@pytest.mark.parametrize('method', ['get', 'post'])
def test_bounded_errors_can_recover(monkeypatch, retry_client, method):
    success = make_response(200)
    request = Mock(side_effect=[requests.Timeout('timeout'), make_response(404), success])
    monkeypatch.setattr(retry_client.session, method, request)
    assert getattr(retry_client, method)('/test', json={'method': 'gdata'}) is success
    assert request.call_count == 3
    request.assert_called_with(retry_client.base_url + '/test', json={'method': 'gdata'},
                               timeout=EHSession.DEFAULT_TIMEOUT)


@pytest.mark.parametrize('method', ['get', 'post'])
@pytest.mark.parametrize('kwargs,expected', [({}, 17), ({'timeout': 15}, 15),
                                           ({'timeout': (5, 30)}, (5, 30))])
def test_request_timeout_defaults_and_overrides(monkeypatch, retry_client, method, kwargs, expected):
    monkeypatch.setattr(EHSession, 'DEFAULT_TIMEOUT', 17)
    success = make_response(200)
    request = Mock(side_effect=[requests.Timeout('timeout'), success])
    monkeypatch.setattr(retry_client.session, method, request)
    assert getattr(retry_client, method)('/test', **kwargs) is success
    assert request.call_count == 2
    assert all(call.kwargs['timeout'] == expected for call in request.call_args_list)


@pytest.mark.parametrize('retry_after,expected', [
    ('12', 12),
    ('0', 0),
    (' 7 ', 7),
    ('Thu, 01 Jan 1970 00:02:00 GMT', 20),
    ('Thu, 01 Jan 1970 00:01:00 GMT', 0),
    (None, 30),
    ('invalid', 30),
    ('', 30),
    ('-1', 30),
])
def test_retry_wait_uses_server_suggestion_or_fallback(monkeypatch, retry_client, retry_after, expected):
    response = make_response(429, retry_after)
    success = make_response(200)
    monkeypatch.setattr(session, 'time', lambda: 100)
    monkeypatch.setattr(retry_client.session, 'get', Mock(side_effect=[response, success]))
    def check_closed_before_sleep(delay):
        response.close.assert_called_once()
    session.sleep.side_effect = check_closed_before_sleep

    assert retry_client.get('/test') is success
    session.sleep.assert_called_once_with(expected)
