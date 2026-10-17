"""Reconstruct galleries by the ten-character SHA1s in their thumbnail links."""

import base64
import hashlib
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag
from PIL import Image, ImageSequence
import zxingcpp

from ehclone.config import config
from ehclone.core.session import ehs
from ehclone.db.crud.gallery import get_gallery_metadata
from ehclone.logger import logger, gallery_logger


ARCHIVE_SUFFIXES = {'.zip', '.cbz', '.rar', '.cbr', '.7z'}
IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif', '.bmp'}

def soup_to_text(tag: Tag, ehs):
    _inline_elements = { 'a', 'abbr', 'acronym', 'audio', 'b', 'bdi', 'bdo', 'big', 'button', 'canvas', 'cite', 'code', 'data', 'datalist', 'del', 'dfn', 'em', 'embed', 'i', 'iframe', 'img', 'input', 'ins', 'kbd', 'label', 'map', 'mark', 'meter', 'noscript', 'object', 'output', 'picture', 'progress', 'q', 'ruby', 's', 'samp', 'script', 'select', 'slot', 'small', 'span', 'strong', 'sub', 'sup', 'svg', 'template', 'textarea', 'time', 'u', 'tt', 'var', 'video', 'wbr', 'br' }
    def _get_text(tag: Tag):
        for child in tag.children:
            if isinstance(child, Tag):
                # if the tag is a block type tag then yield new lines before after
                is_block_element = child.name not in _inline_elements
                if is_block_element:
                    yield "\n"
                if child.name == "br":
                    yield "\n"
                elif child.name == "img":
                    ext = child['src'].split('.')[-1]
                    yield f'data:image/{ext};base64,{base64.b64encode(ehs.get(child["src"]).content).decode()}'
                else:
                    yield from _get_text(child)
                if is_block_element:
                    yield "\n"
            elif isinstance(child, NavigableString):
                yield child
    return "".join(_get_text(tag))

def normalize_filename(name):
    _circle = '〇' # \u3007
    _dot = '・'    # \u30fb
    _star = '☆'    # \u2606
    _heart = '♡'   # \u2661
    normalize_table = {
        '/': '／', '\\': '＼', '?': '？', '"': "'",
        '<': '＜', '>': '＞', '|': '｜', ':': '：', '*': '＊',
        '~': '～', '&': '＆',
        '\u2010': '-', '\u2012': '-', '\u2013': '-', '\u2014': 'ー', '\u2015': '-',
        '[': ' [', ']': '] ', '(': ' (', ')': ') ', '+': ' + ',
        '◯': _circle, '○': _circle, '⚬': _circle, '⚫': _circle, '⬤': _circle, '●': _circle,
        '·': _dot, '˙': _dot, '•': _dot, '∙': _dot, '⋅': _dot,
        '✭': _star, '★': _star, '✩': _star, '✫': _star, '✬': _star, '✮': _star, '✯': _star, '✰': _star,
        '❤': _heart, '❤': _heart, '♥': _heart,
    }

    replace_chars = ''.join(normalize_table.keys())
    replace_pattern = re.compile(f'[{re.escape(replace_chars)}]')

    safename = replace_pattern.sub(lambda m: normalize_table[m.group(0)], name)
    safename = re.sub(r'(\s+)', ' ', safename)
    safename = re.sub(r'\-\-+', 'ー', safename)
    safename = safename.replace(' .', '.')

    safename = safename.strip(' .')

    reserved = {
        'CON', 'PRN', 'AUX', 'NUL',
        *(f'COM{i}' for i in range(1, 10)),
        *(f'LPT{i}' for i in range(1, 10)),
    }
    if safename.upper().split('.')[0] in reserved:
        safename = f'_{name}'

    encoded = safename.encode('utf-8')
    if len(encoded) > 250:
        encoded = encoded[:230] + encoded[-20:]
    safe_name = encoded.decode('utf-8', errors='ignore')

    if name != safe_name:
        logger.info(f'Normalized "{name}" -> "{safe_name}"')

    return safe_name


def _gallery_info(gid: int, token: str):
    gallery = get_gallery_metadata(gid)
    if not gallery:
        # Impossible
        return

    pages = {}
    count = gallery.filecount

    def add_pages(res):
        soup = BeautifulSoup(res.text, 'lxml')
        links = soup.select('#gdt a[href]')
        for link in links:
            match = re.search(r'/s/([0-9a-f]{10})/(\d+)-(\d+)', link['href'])
            if not match or int(match[2]) != gid:
                raise ValueError('Faild parsing page')
            if int(match[3]) in pages:
                raise ValueError('Page number mismatch')
            pages[int(match[3])] = match[1]
        return soup

    current_page_num = 0
    gallery_base_path = f'/g/{gid}/{token}/'
    while len(pages) < count:
        previous_count = len(pages)
        soup = add_pages(ehs.get(f'{gallery_base_path}?p={current_page_num}'))
        if len(pages) == previous_count:
            raise ValueError(f'No image links on page {current_page_num}')
        current_page_num += 1
    if set(pages) != set(range(1, count + 1)):
        raise ValueError('Incomplete online page list')
    pages = [pages[i] for i in range(1, count + 1)]

    tags = [f'{tag.namespace}:{tag.name}' for tag in gallery.tags]
    title = (gallery.title_jpn or gallery.title)
    posted = (gallery.posted_at.strftime('%Y-%m-%d %H:%M'))
    uploader = gallery.uploader
    category = gallery.category.value
    info = [
        f'Title:       {title}',
        f'Upload Time: {posted}',
        f'Uploaded By: {uploader}',
        f'Downloaded:  {datetime.now(timezone.utc):%Y-%m-%d %H:%M}',
        f'Tags:        {", ".join(tags)}',
        f'Category:    {category}',
    ]
    comments = soup.select_one('#comment_0')
    if comments:
        info.extend(["\nUploader's Comments:\n", soup_to_text(comments, ehs)])
    info.append(f'Downloaded by E-Hentai archiver from {config.eh.base_url}{gallery_base_path}\n')
    info = '\n'.join(info)
    return title, tags, pages, info, gallery.posted_at

def _reject_symlinks(directory: Path) -> None:
    if any(path.is_symlink() for path in directory.rglob('*')):
        raise ValueError('Downloaded content contains symbolic links')

def _extract(source: Path, raw: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, raw, dirs_exist_ok=True, symlinks=True)
        _reject_symlinks(raw)
        archives = [p for p in raw.rglob('*') if p.suffix.lower() in ARCHIVE_SUFFIXES and p.is_file()]
    else:
        archives = [source]
    for archive in archives:
        output = raw if archive == source else archive.with_name(archive.name + '.contents')
        output.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                ['7z', 'x', '-y', str(archive), f'-o{output}'],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            logger.info(exc.stdout)
            logger.error(exc.stderr)
            raise
        logger.debug(result.stdout)
        _reject_symlinks(raw)

def _decode_qr(image: Path) -> set[str]:
    """Decode unique, casefolded QR texts across all animation frames."""
    decoded = set()
    with Image.open(image) as opened:
        for frame in ImageSequence.Iterator(opened):
            decoded.update(code.text.casefold() for code in zxingcpp.read_barcodes(
                frame.convert('RGB'),
                formats=zxingcpp.BarcodeFormat.QRCode,
                text_mode=zxingcpp.TextMode.Plain,
            ))
    return decoded


def validate(gid: int, token: str, source: Path) -> bool:
    """Return False for incomplete/corrupt downloads; raise on operational errors.
    """
    log = gallery_logger(gid, token)
    source = Path(source).resolve()
    if not source.exists():
        log.error('Download missing: {}', source)
        return False

    title, tags, checksums, info, posted_at = _gallery_info(gid, token)
    suffix = f' [{gid}]{config.archiver.extension}'
    # Reserve space for the full ID and extension even when the title is long.
    title_bytes = normalize_filename(title).encode('utf-8')
    filename_title = title_bytes[:250 - len(suffix.encode('utf-8'))].decode('utf-8', errors='ignore').rstrip()
    destination = (
        config.dest.resolve()
        / f'{posted_at:%Y}'
        / f'{posted_at:%m}'
        / f'{filename_title}{suffix}'
    )

    config.cache.mkdir(parents=True, exist_ok=True)
    # make sure cache is removed even if validation failed
    with tempfile.TemporaryDirectory(prefix=f'{gid}_{token}-', dir=config.cache) as directory:
        cache = Path(directory).resolve()
        raw = cache / 'raw'
        raw.mkdir()
        try:
            _extract(source, raw)
        except (subprocess.CalledProcessError, ValueError):
            log.exception('Extraction failed.')
            return False

        local = {}
        for image in sorted(raw.rglob('*')):
            if image.is_file() and image.suffix.lower() in IMAGE_SUFFIXES:
                with image.open('rb') as stream:
                    checksum = hashlib.file_digest(stream, 'sha1').hexdigest()[:10]
                local.setdefault(checksum, []).append(image)

        missing = [i for i, checksum in enumerate(checksums, 1) if checksum not in local]
        if missing:
            log.error('Missing pages {}', missing)
            return False

        pages = cache / 'pages'
        pages.mkdir()
        out_of_order = 'other:out of order' in tags
        blacklisted = {checksum.lower()[:10] for checksum in config.archiver.blacklist.sha1}
        qr_keywords = {keyword.casefold() for keyword in config.archiver.blacklist.qr}
        used = set()
        for index, checksum in enumerate(checksums, 1):
            if checksum in blacklisted:
                continue
            images = local[checksum]
            image = next((image for image in images if image not in used), images[0])
            used.add(image)
            if qr_keywords:
                try:
                    decoded = _decode_qr(image)
                except (OSError, ValueError):
                    log.exception('QR scan failed for {}', image.relative_to(raw))
                    return False
                matches = [text for text in decoded if any(keyword in text for keyword in qr_keywords)]
                for text in matches:
                    log.warning('Filtered {}: blacklisted QR code: {}', image.relative_to(raw), text)
                if matches:
                    continue
            name = '_'.join(image.relative_to(raw).parts) if out_of_order else f'{index:04d}{image.suffix}'
            target = pages / name
            target.hardlink_to(image)
        (pages / 'galleryinfo.txt').write_text(info, encoding='utf-8')

        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f'{gid}-staging-', dir=destination.parent) as staging:
            output = Path(staging).resolve() / destination.name
            command = []
            for argument in config.archiver.cmd:
                if argument == '$dst':
                    command.append(str(output))
                elif argument == '$src':
                    command.append('.')
                else:
                    command.append(argument)
            try:
                result = subprocess.run(
                    command,
                    cwd=pages,
                    check=True,
                    text=True,
                    capture_output=True
                )
            except subprocess.CalledProcessError as exc:
                log.info(exc.stdout)
                log.error(exc.stderr)
                raise
            log.debug(result.stdout)
            output.replace(destination)
    log.info('Archived {} to {}', title, destination)
    return True
