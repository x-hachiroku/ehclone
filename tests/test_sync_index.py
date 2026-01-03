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


def test_metadata_failure_does_not_report_end_of_index(monkeypatch):
    monkeypatch.setattr(sync_index.ehs, 'get', lambda url: SimpleNamespace(text=
        '<div class="itg"><a href="/g/20/abc/">A</a></div>'))
    monkeypatch.setattr(sync_index.ehs, 'gdata', lambda pairs: [])
    with pytest.raises(RuntimeError, match='Could not retrieve gallery metadata'):
        sync_index.sync_page(10)


@pytest.mark.parametrize('include_expunged', [False, True])
def test_sync_index_refreshes_metadata_even_without_new_galleries(monkeypatch, include_expunged):
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


@pytest.mark.parametrize('resume', [False, True])
@pytest.mark.parametrize('include_expunged', [False, True])
def test_index_scans_resume_and_advance_independent_cursors(monkeypatch, resume, include_expunged):
    monkeypatch.setattr(sync_index.config.eh, 'resume', resume)
    monkeypatch.setattr(sync_index.config.eh, 'include_expunged', include_expunged)
    last_gid = Mock(side_effect=lambda expunged: 200 if expunged else 100)
    monkeypatch.setattr(sync_index, 'get_last_gid', last_gid)
    pages = {False: iter([150, -1]), True: iter([250, -1])}
    page = Mock(side_effect=lambda prev, expunged: next(pages[expunged]))
    monkeypatch.setattr(sync_index, 'sync_page', page)
    monkeypatch.setattr(sync_index, 'get_galleries_for_metadata_refresh', Mock(return_value=[]))
    sync_index.run()

    cursors = {mode: [call.args[0] for call in page.call_args_list
                      if call.kwargs['expunged'] == mode] for mode in (False, True)}
    assert cursors[False] == [100 if resume else 1, 150]
    if include_expunged:
        assert cursors[True] == [200 if resume else 1, 250]
    else:
        assert cursors[True] == []
