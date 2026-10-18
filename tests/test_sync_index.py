from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from ehclone.core import sync_index


def test_index_links_inside_tbody_and_multiple_display_modes(monkeypatch):
    monkeypatch.setattr(sync_index.ehs, 'get', lambda url: SimpleNamespace(text="""
        <table class="itg"><tbody><tr>
        <td class="gl3c"><a href="/g/20/abc/">A</a></td>
        <td><a href="/g/20/abc/">same gallery</a></td>
        <td class="gl2e"><div><a href="/g/30/def/">B</a></div></td>
        <td><a href="/other">not a gallery</a></td>
        </tr></tbody></table>
    """))
    gdata = Mock(return_value=[{'gid': 20, 'token': 'abc'}, {'gid': 30, 'token': 'def'}])
    insert = Mock()
    monkeypatch.setattr(sync_index.ehs, 'gdata', gdata)
    monkeypatch.setattr(sync_index, 'insert_galleries', insert)
    assert sync_index.sync_page(10) == 30
    gdata.assert_called_once_with([[20, 'abc'], [30, 'def']])
    insert.assert_called_once()
    assert sync_index.sync_page(30) == -1


@pytest.mark.parametrize('recheck_until', [None, 30])
def test_metadata_failure_does_not_report_end_of_index(monkeypatch, recheck_until):
    monkeypatch.setattr(sync_index, 'get_existing_gids', Mock(return_value=set()))
    monkeypatch.setattr(sync_index.ehs, 'get', lambda url: SimpleNamespace(text=
        '<div class="itg"><a href="/g/20/abc/">A</a></div>'))
    monkeypatch.setattr(sync_index.ehs, 'gdata', lambda pairs: [])
    with pytest.raises(RuntimeError, match='Could not retrieve gallery metadata'):
        sync_index.sync_page(10, recheck_until=recheck_until)


@pytest.mark.parametrize('include_expunged', [False, True])
@pytest.mark.parametrize('recheck', [0, 10])
def test_sync_index_refreshes_metadata_even_without_new_galleries(monkeypatch, include_expunged, recheck):
    monkeypatch.setattr(sync_index.config.eh, 'recheck', recheck)
    monkeypatch.setattr(sync_index.config.eh, 'include_expunged', include_expunged)
    monkeypatch.setattr(sync_index, 'get_last_gid', Mock(return_value=100))
    page = Mock(return_value=-1)
    monkeypatch.setattr(sync_index, 'sync_page', page)
    batches = [[[20, 'abc']], [[30, 'def']], []]
    pending = Mock(side_effect=batches)
    monkeypatch.setattr(sync_index, 'get_galleries_for_metadata_refresh', pending)
    metadata = [[{'gid': 20, 'token': 'abc'}], [{'gid': 30, 'token': 'def'}]]
    gdata = Mock(side_effect=metadata)
    monkeypatch.setattr(sync_index.ehs, 'gdata', gdata)
    insert = Mock()
    monkeypatch.setattr(sync_index, 'insert_galleries', insert)

    sync_index.run()

    assert {call.kwargs['expunged'] for call in page.call_args_list} == (
        {False, True} if include_expunged else {False})
    assert gdata.call_args_list == [call(batch) for batch in batches[:-1]]
    assert insert.call_args_list == [call(batch) for batch in metadata]


def test_refresh_failure_does_not_write_metadata(monkeypatch):
    monkeypatch.setattr(sync_index.config.eh, 'include_expunged', False)
    monkeypatch.setattr(sync_index, 'get_last_gid', Mock(return_value=100))
    monkeypatch.setattr(sync_index, 'sync_page', Mock(return_value=-1))
    monkeypatch.setattr(sync_index, 'get_galleries_for_metadata_refresh',
                        Mock(return_value=[[20, 'abc']]))
    monkeypatch.setattr(sync_index.ehs, 'gdata', Mock(return_value=[]))
    insert = Mock()
    monkeypatch.setattr(sync_index, 'insert_galleries', insert)

    with pytest.raises(RuntimeError, match='Could not retrieve gallery metadata for refresh'):
        sync_index.run()

    insert.assert_not_called()


@pytest.mark.parametrize('recheck', [-1, 0, 10, 100, 300])
@pytest.mark.parametrize('include_expunged', [False, True])
def test_index_scans_start_and_advance_independent_cursors(monkeypatch, recheck, include_expunged):
    monkeypatch.setattr(sync_index.config.eh, 'recheck', recheck)
    monkeypatch.setattr(sync_index.config.eh, 'include_expunged', include_expunged)
    last_gid = Mock(side_effect=lambda expunged: 200 if expunged else 100)
    monkeypatch.setattr(sync_index, 'get_last_gid', last_gid)
    pages = {False: iter([150, -1]), True: iter([250, -1])}
    page = Mock(side_effect=lambda prev, expunged, recheck_until: next(pages[expunged]))
    monkeypatch.setattr(sync_index, 'sync_page', page)
    refresh = Mock(return_value=[])
    monkeypatch.setattr(sync_index, 'get_galleries_for_metadata_refresh', refresh)
    sync_index.run()

    cursors = {mode: [call.args[0] for call in page.call_args_list
                      if call.kwargs['expunged'] == mode] for mode in (False, True)}
    assert cursors[False] == [min(recheck, 100) if recheck > 0 else 100, 150]
    if include_expunged:
        assert cursors[True] == [min(recheck, 200) if recheck > 0 else 200, 250]
    else:
        assert cursors[True] == []
    assert last_gid.call_args_list == ([call(expunged=False), call(expunged=True)]
                                      if include_expunged else [call(expunged=False)])
    for page_call in page.call_args_list:
        original_gid = 200 if page_call.kwargs['expunged'] else 100
        assert page_call.kwargs['recheck_until'] == (original_gid if recheck > 0 else None)
    refresh.assert_called_once()


@pytest.mark.parametrize('existing', [{20, 30}, {30}, set()])
@pytest.mark.parametrize('expunged', [False, True])
def test_recheck_fetches_only_missing_galleries_and_advances_past_known_ones(
        monkeypatch, existing, expunged):
    monkeypatch.setattr(sync_index.ehs, 'get', Mock(return_value=SimpleNamespace(text='''
        <div class="itg">
        <a href="/g/20/abc/">A</a><a href="/g/20/abc/">A again</a>
        <a href="/g/30/def/">B</a>
        </div>
    ''')))
    lookup = Mock(return_value=existing)
    monkeypatch.setattr(sync_index, 'get_existing_gids', lookup)
    missing = [[gid, token] for gid, token in [[20, 'abc'], [30, 'def']] if gid not in existing]
    metadata = [{'gid': gid, 'token': token} for gid, token in missing]
    gdata = Mock(return_value=metadata)
    insert = Mock()
    log = Mock()
    monkeypatch.setattr(sync_index.ehs, 'gdata', gdata)
    monkeypatch.setattr(sync_index, 'insert_galleries', insert)
    monkeypatch.setattr(sync_index, 'gallery_logger', log)

    assert sync_index.sync_page(10, expunged=expunged, recheck_until=30) == 30

    lookup.assert_called_once_with([20, 30])
    assert log.call_args_list == [call(gid, token) for gid, token in missing]
    assert log.return_value.warning.call_count == len(missing)
    if missing:
        gdata.assert_called_once_with(missing)
        insert.assert_called_once_with(metadata)
    else:
        gdata.assert_not_called()
        insert.assert_not_called()
        assert sync_index.sync_page(30, expunged=expunged, recheck_until=30) == -1


def test_recheck_empty_search_does_not_query_database_or_api(monkeypatch):
    monkeypatch.setattr(sync_index.ehs, 'get', Mock(return_value=SimpleNamespace(text='')))
    lookup = Mock()
    gdata = Mock()
    monkeypatch.setattr(sync_index, 'get_existing_gids', lookup)
    monkeypatch.setattr(sync_index.ehs, 'gdata', gdata)

    assert sync_index.sync_page(10, recheck_until=30) == -1
    lookup.assert_not_called()
    gdata.assert_not_called()


@pytest.mark.parametrize('existing', [{20}, set()])
def test_recheck_boundary_page_and_later_pages_keep_normal_sync(monkeypatch, existing):
    monkeypatch.setattr(sync_index.ehs, 'get', Mock(side_effect=[
        SimpleNamespace(text='''<div class="itg">
            <a href="/g/20/abc/">At original last GID</a>
            <a href="/g/30/def/">New gallery</a>
        </div>'''),
        SimpleNamespace(text='<div class="itg"><a href="/g/40/fed/">Newer gallery</a></div>'),
    ]))
    lookup = Mock(return_value=existing)
    log = Mock()
    gdata = Mock(side_effect=lambda pairs: [{'gid': gid, 'token': token} for gid, token in pairs])
    insert = Mock()
    monkeypatch.setattr(sync_index, 'get_existing_gids', lookup)
    monkeypatch.setattr(sync_index, 'gallery_logger', log)
    monkeypatch.setattr(sync_index.ehs, 'gdata', gdata)
    monkeypatch.setattr(sync_index, 'insert_galleries', insert)

    assert sync_index.sync_page(10, recheck_until=20) == 30
    assert sync_index.sync_page(30, recheck_until=20) == 40

    lookup.assert_called_once_with([20])
    first_batch = [[30, 'def']] if existing else [[20, 'abc'], [30, 'def']]
    assert gdata.call_args_list == [call(first_batch), call([[40, 'fed']])]
    assert insert.call_count == 2
    if existing:
        log.assert_not_called()
    else:
        log.assert_called_once_with(20, 'abc')
        log.return_value.warning.assert_called_once()
