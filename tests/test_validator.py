import hashlib
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image
import zxingcpp

from ehclone.config import config
from ehclone.core import validator
from ehclone.core.download_manager import DownloadManager
from ehclone.db.entities import Category, DownloadState as State, Gallery, Tag


def sha(data):
    return hashlib.sha1(data).hexdigest()[:10]


@pytest.fixture(autouse=True)
def stored_gallery(monkeypatch):
    gallery = Gallery(
        gid=123, token='token', title='Test title', title_jpn='', filecount=1,
        posted_at=datetime(2026, 1, 1), uploader='Uploader', category=Category.MANGA,
    )
    gallery.tags = []
    monkeypatch.setattr(validator, 'get_gallery_metadata', Mock(return_value=gallery))
    return gallery


def gallery_html(hashes, offset=0):
    links = ''.join(f'<a href="/s/{checksum}/123-{i}"></a>'
                    for i, checksum in enumerate(hashes, offset + 1))
    return f'<div id="gdt">{links}</div>'


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'cache', tmp_path / 'cache')
    monkeypatch.setattr(config, 'dest', tmp_path / 'dest')
    monkeypatch.setattr(config.archiver, 'cmd', ['7z', 'a', '-tzip', '$dst', '$src'])
    monkeypatch.setattr(config.archiver, 'extension', '.cbz')
    return tmp_path


def mock_gallery(monkeypatch, hashes, tags=()):
    gallery = validator.get_gallery_metadata.return_value
    gallery.filecount = len(hashes)
    gallery.tags = [Tag(namespace=namespace, name=name) for namespace, name in tags]
    monkeypatch.setattr(validator.ehs, 'get', lambda url:
                        SimpleNamespace(text=gallery_html(hashes)))


@pytest.mark.parametrize('tags', [
    [],
    [('artist', 'artist name')],
    [('group', 'group name')],
    [('artist', 'artist name'), ('group', 'group name')],
])
def test_archives_use_posting_date_without_tag_subdirectories(
        paths, monkeypatch, stored_gallery, tags):
    source = paths / 'torrent'
    source.mkdir()
    (source / 'page.jpg').write_bytes(b'a')
    stored_gallery.posted_at = datetime(2024, 2, 29, 23, 59)
    mock_gallery(monkeypatch, [sha(b'a')], tags)

    def package(command, cwd, check, text, capture_output):
        with zipfile.ZipFile(command[3], 'w') as archive:
            for page in Path(cwd).iterdir():
                archive.write(page, page.name)
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(validator.subprocess, 'run', package)
    assert validator.validate(123, 'token', source)
    output = config.dest / '2024' / '02' / 'Test title [123].cbz'
    assert list(config.dest.rglob('*.cbz')) == [output]
    assert list(output.parent.iterdir()) == [output]
    with zipfile.ZipFile(output) as archive:
        assert archive.read('0001.jpg') == b'a'
        assert 'Upload Time: 2024-02-29 23:59' in archive.read('galleryinfo.txt').decode()


@pytest.mark.parametrize('out_of_order', [False, True])
def test_missing_pages_fail_even_when_blacklisted(paths, monkeypatch, out_of_order):
    source = paths / 'torrent'
    source.mkdir()
    (source / 'a.jpg').write_bytes(b'a')
    tags = [('other', 'out of order')] if out_of_order else []
    mock_gallery(monkeypatch, [sha(b'a'), sha(b'b')], tags)
    monkeypatch.setattr(config.archiver.blacklist, 'sha1', [sha(b'b')])
    assert validator.validate(123, 'token', source) is False
    assert not config.dest.exists()
    assert list(config.cache.iterdir()) == []
    assert (source / 'a.jpg').read_bytes() == b'a'


def test_paginated_page_hashes_follow_database_count(monkeypatch, stored_gallery):
    hashes = [sha(b'a'), sha(b'b'), sha(b'a'), sha(b'c'), sha(b'd')]
    stored_gallery.filecount = len(hashes)
    urls = []
    def get(url):
        urls.append(url)
        index = len(urls) - 1
        assert url == f'/g/123/token/?p={index}'
        assert index < 3
        offset = index * 2
        return SimpleNamespace(text=gallery_html(hashes[offset:offset + 2], offset=offset))
    monkeypatch.setattr(validator.ehs, 'get', get)
    assert validator._gallery_info(123, 'token')[2] == hashes
    assert urls == [f'/g/123/token/?p={index}' for index in range(3)]


@pytest.mark.skipif(shutil.which('7z') is None, reason='7z is required')
def test_corrupt_archive_fails_validation(paths, monkeypatch):
    source = paths / 'bad.cbz'
    source.write_bytes(b'not a zip')
    mock_gallery(monkeypatch, [sha(b'a')])
    assert validator.validate(123, 'token', source) is False

    assert list(config.cache.iterdir()) == []
    assert source.read_bytes() == b'not a zip'


def test_archiver_failure_does_not_replace_previous_destination(paths, monkeypatch):
    source = paths / 'torrent'
    source.mkdir()
    (source / 'page.jpg').write_bytes(b'a')
    mock_gallery(monkeypatch, [sha(b'a')])
    output = config.dest / '2026' / '01' / 'Test title [123].cbz'
    output.parent.mkdir(parents=True)
    output.write_bytes(b'existing archive')
    monkeypatch.setattr(config.archiver, 'cmd', ['missing-archiver-executable', '$dst', '$src'])
    with pytest.raises(FileNotFoundError):
        validator.validate(123, 'token', source)
    assert output.read_bytes() == b'existing archive'
    assert list(output.parent.iterdir()) == [output]
    assert list(config.cache.iterdir()) == []
    assert (source / 'page.jpg').read_bytes() == b'a'


@pytest.mark.skipif(shutil.which('7z') is None, reason='7z is required')
def test_blacklist_excludes_only_after_all_pages_are_verified(paths, monkeypatch):
    source = paths / 'torrent'
    source.mkdir()
    (source / 'a.jpg').write_bytes(b'a')
    (source / 'b.jpg').write_bytes(b'b')
    mock_gallery(monkeypatch, [sha(b'a'), sha(b'b')])
    monkeypatch.setattr(config.archiver.blacklist, 'sha1', [sha(b'a')])
    assert validator.validate(123, 'token', source)
    with zipfile.ZipFile(config.dest / '2026' / '01' / 'Test title [123].cbz') as archive:
        assert set(archive.namelist()) == {'0002.jpg', 'galleryinfo.txt'}


def qr_image(text):
    code = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode)
    return Image.fromarray(code.to_image(scale=5)).convert('RGB')


@pytest.mark.parametrize('suffix', sorted(validator.IMAGE_SUFFIXES))
def test_decode_all_qr_codes(tmp_path, suffix):
    texts = ['https://example.org/safe', 'https://example.org/BLoCkEd']
    codes = [qr_image(text) for text in texts]
    canvas = Image.new('RGB', (500, 250), 'white')
    for index, code in enumerate(codes):
        canvas.paste(code, (index * 250 + 20, 20))
    path = tmp_path / f'multiple{suffix}'
    canvas.save(path)
    assert validator._decode_qr(path) == {text.casefold() for text in texts}


def test_decode_qr_in_later_animation_frame(tmp_path):
    text = 'https://example.org/blocked'
    code = qr_image(text)
    first = Image.new('RGB', code.size, 'white')
    path = tmp_path / 'animated.gif'
    first.save(path, save_all=True, append_images=[qr_image(text.upper()), code],
               duration=100, loop=0)
    assert validator._decode_qr(path) == {text}


@pytest.mark.parametrize('out_of_order', [False, True])
def test_qr_blacklist_excludes_images_and_logs_decoded_text(paths, monkeypatch, out_of_order):
    source = paths / 'torrent'
    source.mkdir()
    Image.new('RGB', (250, 250), 'white').save(source / 'blank.png')
    qr_image('https://example.org/safe').save(source / 'safe.png')
    texts = ['https://example.org/BLoCkEd', 'https://example.org/Straße']
    canvas = Image.new('RGB', (500, 250), 'white')
    for index, text in enumerate(texts):
        canvas.paste(qr_image(text), (index * 250 + 20, 20))
    canvas.save(source / 'blocked.png')
    (source / 'sha1.jpg').write_bytes(b'sha1 blocked')
    originals = {name: (source / name).read_bytes()
                 for name in ['blank.png', 'safe.png', 'blocked.png', 'sha1.jpg']}
    tags = [('other', 'out of order')] if out_of_order else []
    mock_gallery(monkeypatch, [sha(data) for data in originals.values()], tags)
    monkeypatch.setattr(config.archiver.blacklist, 'sha1', [sha(b'sha1 blocked')])
    monkeypatch.setattr(config.archiver.blacklist, 'qr', ['unused', 'bLOCKed', 'STRASSE'])

    def package(command, cwd, check, text, capture_output):
        with zipfile.ZipFile(command[3], 'w') as archive:
            for page in Path(cwd).iterdir():
                archive.write(page, page.name)
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(validator.subprocess, 'run', package)
    warnings = []
    sink = validator.logger.add(warnings.append, level='WARNING')
    try:
        assert validator.validate(123, 'token', source)
    finally:
        validator.logger.remove(sink)
    assert len(warnings) == 2
    for text in texts:
        assert any(text.casefold() in message and message.record['level'].name == 'WARNING'
                   for message in warnings)
    with zipfile.ZipFile(config.dest / '2026' / '01' / 'Test title [123].cbz') as archive:
        names = ['blank.png', 'safe.png'] if out_of_order else ['0001.png', '0002.png']
        assert set(archive.namelist()) == {*names, 'galleryinfo.txt'}
        for name, original in zip(names, list(originals.values())[:2]):
            assert archive.read(name) == original
    assert {name: (source / name).read_bytes() for name in originals} == originals


def test_qr_scanning_waits_for_complete_gallery(paths, monkeypatch):
    source = paths / 'torrent'
    source.mkdir()
    (source / 'page.jpg').write_bytes(b'a')
    mock_gallery(monkeypatch, [sha(b'a'), sha(b'missing')])
    monkeypatch.setattr(config.archiver.blacklist, 'qr', ['blocked'])
    decode = Mock(side_effect=AssertionError('must verify completeness first'))
    monkeypatch.setattr(validator, '_decode_qr', decode)
    assert not validator.validate(123, 'token', source)
    decode.assert_not_called()


def test_unreadable_qr_image_does_not_replace_existing_archive(paths, monkeypatch):
    source = paths / 'torrent'
    source.mkdir()
    (source / 'page.jpg').write_bytes(b'not an image')
    mock_gallery(monkeypatch, [sha(b'not an image')])
    monkeypatch.setattr(config.archiver.blacklist, 'qr', ['blocked'])
    output = config.dest / '2026' / '01' / 'Test title [123].cbz'
    output.parent.mkdir(parents=True)
    output.write_bytes(b'existing archive')
    assert not validator.validate(123, 'token', source)
    assert output.read_bytes() == b'existing archive'
    assert list(config.cache.iterdir()) == []


def test_source_symlink_is_rejected_without_copying_its_target(paths, monkeypatch):
    private = paths / 'private.jpg'
    private.write_bytes(b'private')
    source = paths / 'torrent'
    source.mkdir()
    (source / 'a.jpg').symlink_to(private)
    mock_gallery(monkeypatch, [sha(b'private')])
    assert not validator.validate(123, 'token', source)
    assert not config.dest.exists()


def test_download_metadata_uses_utc_without_offset_and_keeps_database_time(monkeypatch):
    mock_gallery(monkeypatch, [sha(b'a')])
    before = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
    info = validator._gallery_info(123, 'token')[3]
    after = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
    downloaded = next(line.split(':', 1)[1].strip() for line in info.splitlines()
                      if line.startswith('Downloaded:'))
    downloaded = datetime.fromisoformat(downloaded)
    assert downloaded.tzinfo is None
    assert before <= downloaded <= after
    assert 'Upload Time: 2026-01-01 00:00\n' in info


def test_gallery_info_uses_database_metadata_and_fetches_page_hashes(monkeypatch):
    hashes = [sha(b'a'), sha(b'b'), sha(b'c')]
    gallery = Gallery(
        gid=123, title='Stored English', title_jpn='Stored Japanese', filecount=3,
        posted_at=datetime(2025, 12, 31, 23, 45), uploader='Stored uploader', category=Category.MANGA,
    )
    gallery.tags = [Tag(namespace='other', name='out of order')]
    lookup = Mock(return_value=gallery)
    monkeypatch.setattr(validator, 'get_gallery_metadata', lookup)
    urls = []

    def get(url):
        urls.append(url)
        page_hashes = hashes[2:] if '?p=1' in url else hashes[:2]
        offset = 2 if '?p=1' in url else 0
        # Only page links and comments are needed from the website.
        links = ''.join(f'<a href="/s/{checksum}/123-{index}"></a>'
                        for index, checksum in enumerate(page_hashes, offset + 1))
        return SimpleNamespace(text=f'<div id="gdt">{links}</div>'
                                    '<div id="comment_0">Uploader comment</div>')

    monkeypatch.setattr(validator.ehs, 'get', get)
    title, tags, checksums, info, posted_at = validator._gallery_info(123, 'token')
    lookup.assert_called_once_with(123)
    assert title == 'Stored Japanese'
    assert tags == ['other:out of order']
    assert checksums == hashes
    assert posted_at == datetime(2025, 12, 31, 23, 45)
    assert 'Upload Time: 2025-12-31 23:45\n' in info
    assert 'Uploaded By: Stored uploader\n' in info
    assert 'Category:    Manga\n' in info
    assert 'Uploader comment' in info
    assert urls == ['/g/123/token/?p=0', '/g/123/token/?p=1']


def test_repeated_online_page_numbers_are_rejected(monkeypatch, stored_gallery):
    mock_gallery(monkeypatch, [sha(b'a')])
    stored_gallery.filecount = 2
    with pytest.raises(ValueError, match='Page number mismatch'):
        validator._gallery_info(123, 'token')


@pytest.mark.parametrize('has_first_page', [False, True])
def test_empty_gallery_page_stops_pagination(monkeypatch, stored_gallery, has_first_page):
    stored_gallery.filecount = 2
    responses = [SimpleNamespace(text='<html>Gallery unavailable</html>')]
    if has_first_page:
        responses.insert(0, SimpleNamespace(text=gallery_html([sha(b'a')])))
    get = Mock(side_effect=responses)
    monkeypatch.setattr(validator.ehs, 'get', get)
    with pytest.raises(ValueError, match=f'^No image links on page {int(has_first_page)}$'):
        validator._gallery_info(123, 'token')
    assert get.call_count == len(responses)


@pytest.mark.skipif(shutil.which('7z') is None, reason='7z is required')
@pytest.mark.parametrize('title', ['Same title', '長いタイトル' * 100])
def test_same_title_galleries_keep_separate_archives(paths, monkeypatch, title):
    source = paths / 'torrent'
    source.mkdir()
    gids = [123456789012345678, 223456789012345678]
    for gid, content in zip(gids, [b'first gallery', b'second gallery']):
        (source / 'page.jpg').write_bytes(content)
        monkeypatch.setattr(validator, '_gallery_info', Mock(
            return_value=(title, [], [sha(content)], 'info', datetime(2026, 1, 1))))
        assert validator.validate(gid, 'token', source)

    archives = list((config.dest / '2026' / '01').iterdir())
    assert len(archives) == 2
    for gid, content in zip(gids, [b'first gallery', b'second gallery']):
        output = next(path for path in archives if path.name.endswith(f' [{gid}].cbz'))
        assert len(output.name.encode('utf-8')) <= 250
        with zipfile.ZipFile(output) as archive:
            assert archive.read('0001.jpg') == content


def test_online_page_numbers_must_cover_database_count(monkeypatch):
    mock_gallery(monkeypatch, [sha(b'a'), sha(b'b')])
    monkeypatch.setattr(validator.ehs, 'get', Mock(return_value=SimpleNamespace(
        text=gallery_html([sha(b'a'), sha(b'b')], offset=1))))
    with pytest.raises(ValueError, match='Incomplete online page list'):
        validator._gallery_info(123, 'token')


@pytest.mark.skipif(shutil.which('7z') is None, reason='7z is required')
@pytest.mark.parametrize('backend', ['aria2', 'qbit'])
def test_download_packaging_blacklist_and_source_lifecycle(paths, monkeypatch, backend):
    local = paths / backend
    local.mkdir()
    monkeypatch.setattr(getattr(config, backend), 'local_dir', local)
    source = local / '123_token.cbz'
    gallery = Gallery(gid=123, token='token')
    manager = DownloadManager()
    manager.aria2 = Mock()
    manager.qbit = Mock()
    if backend == 'qbit':
        torrent_dir = local / '123_token'
        torrent_dir.mkdir()
        source = torrent_dir / 'original.cbz'
        gallery.bittorrent = State.DOWNLOADING
        gallery.bittorrent_task_id = 'abc'
        manager.qbit.get_torrents_info.return_value = [SimpleNamespace(hash='abc', progress=1)]
        manager.qbit.matches_directory.return_value = True
    else:
        gallery.archiver = State.DOWNLOADING
        gallery.archiver_task_id = 'abc'
        manager.aria2.get_status.return_value = {'status': 'complete'}
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('a.jpg', b'a')
        archive.writestr('b.jpg', b'b')
    original = source.read_bytes()
    mock_gallery(monkeypatch, [sha(b'a'), sha(b'b')])
    monkeypatch.setattr(config.archiver.blacklist, 'sha1', [sha(b'a')])
    manager._check(gallery)
    assert source.read_bytes() == original
    assert not source.with_name(source.name + '.contents').exists()
    with zipfile.ZipFile(config.dest / '2026' / '01' / 'Test title [123].cbz') as archive:
        assert set(archive.namelist()) == {'0002.jpg', 'galleryinfo.txt'}
        assert archive.read('0002.jpg') == b'b'
    assert list(config.cache.iterdir()) == []
    manager._cleanup_archive(gallery)
    if backend == 'qbit':
        assert gallery.bittorrent == State.COMPLETED
        assert source.read_bytes() == original
        manager.qbit.delete_torrents.assert_not_called()
    else:
        assert gallery.archiver == State.COMPLETED
        assert not source.exists()


@pytest.mark.parametrize('archiver', ['7z', 'rar', 'zip'])
def test_directory_packaging_includes_nested_pages_and_excludes_blacklist(paths, monkeypatch, archiver):
    if shutil.which('7z') is None or shutil.which(archiver) is None:
        pytest.skip('7z and the selected archiver are required')
    command = {
        '7z': ['7z', 'a', '-tzip'],
        'rar': ['rar', 'a', '-ep', '-r'],
        'zip': ['zip', '-0', '--junk-sfx', '--junk-paths', '--test', '-r'],
    }[archiver]
    monkeypatch.setattr(config.archiver, 'cmd', [*command, '$dst', '$src'])
    source = paths / 'torrent'
    (source / 'nested').mkdir(parents=True)
    (source / 'nested' / 'keep.jpg').write_bytes(b'keep')
    (source / 'nested' / 'blocked.jpg').write_bytes(b'blocked')
    (source / 'unmatched.jpg').write_bytes(b'unmatched')
    mock_gallery(monkeypatch, [sha(b'keep'), sha(b'blocked')],
                 [('other', 'out of order')])
    monkeypatch.setattr(config.archiver.blacklist, 'sha1', [sha(b'blocked')])
    assert validator.validate(123, 'token', source)
    extracted = paths / 'extracted'
    output = config.dest / '2026' / '01' / 'Test title [123].cbz'
    subprocess.run(['7z', 'x', '-y', str(output), f'-o{extracted}'],
                   check=True, stdout=subprocess.DEVNULL)
    files = {p.relative_to(extracted).as_posix() for p in extracted.rglob('*') if p.is_file()}
    image_name = 'nested_keep.jpg'
    assert files == {image_name, 'galleryinfo.txt'}
    assert (extracted / image_name).read_bytes() == b'keep'
