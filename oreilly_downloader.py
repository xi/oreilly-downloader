# /// script
# dependencies = [
#   "aiohttp",
#   "lxml",
# ]
# ///

import argparse
import asyncio
import xml.etree.ElementTree as ET
import zipfile
from html import escape

import aiohttp
from lxml import etree
from lxml import html as lhtml

BASE_URL = 'https://learning.oreilly.com'

CONTAINER = b"""<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
    <rootfiles>
        <rootfile full-path="EPUB/content.opf" media-type="application/oebps-package+xml"/>
    </rootfiles>
</container>
"""  # noqa

XHTML_SKELETON = (
    '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml" '
    'xmlns:epub="http://www.idpf.org/2007/ops">\n'
    '<head>\n<meta charset="utf-8"/>\n<title>{title}</title>\n'
    '<link rel="stylesheet" type="text/css" href="epub.css"/>\n</head>\n'
    '<body{bodyattrs}>\n{body}\n</body>\n</html>\n'
)


def clean_injected(doc):
    """Strip bot-protection junk Akamai injects into served HTML.

    Removes <script>/<noscript> tags, <link> elements pointing to absolute
    (off-container) URLs, and the hidden ``sec-overlay`` challenge container.
    """
    etree.strip_elements(doc, 'script', 'noscript', with_tail=False)
    for link in doc.findall('.//link'):
        if link.get('href', '').startswith(('/', '//', 'http:', 'https:')):
            link.getparent().remove(link)
    for el in list(doc.iter()):
        if el.get('id') in ('sec-overlay', 'sec-container'):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)


def to_xhtml(content, fallback_title):
    """Wrap an O'Reilly HTML fragment into a valid standalone XHTML document.

    The files API serves content as bare fragments (``<div id="sbo-rt-content">
    …</div>``) or, for the nav document, a full page with HTML5 void elements
    and unbound ``epub:`` attributes. Strict readers (e.g. Apple Books) reject
    both. Parsing as HTML and re-serializing as XML self-closes void elements
    and normalizes entities; the wrapper binds the XHTML and epub namespaces.
    """
    # O'Reilly fragments are UTF-8 but carry no charset declaration; lxml's
    # HTML parser would otherwise default to Latin-1 and mangle curly quotes.
    doc = lhtml.fromstring(content, parser=lhtml.HTMLParser(encoding='utf-8'))
    clean_injected(doc)

    if doc.tag == 'html':
        head, body = doc.find('head'), doc.find('body')
        title = fallback_title
        if head is not None:
            title_el = head.find('title')
            if title_el is not None and title_el.text:
                title = title_el.text
        bodyattrs = ''.join(f' {k}="{escape(v)}"' for k, v in body.attrib.items())
        inner = (body.text or '') + ''.join(
            etree.tostring(c, encoding='unicode') for c in body)
    else:
        title = fallback_title
        h1 = doc.find('.//h1')
        if h1 is not None:
            title = ''.join(h1.itertext()).strip() or fallback_title
        bodyattrs = ''
        inner = etree.tostring(doc, encoding='unicode')

    return XHTML_SKELETON.format(
        title=escape(title), bodyattrs=bodyattrs, body=inner).encode('utf-8')


async def check_auth(session):
    url = BASE_URL + '/api/v1/user-preferences/'
    async with session.get(url, raise_for_status=False) as r:
        return r.ok


async def fetch_book(book_id, zfh, session):
    root_path = f'/api/v2/epubs/urn:orm:book:{book_id}/files/'
    b_root_path = root_path.encode('utf-8')

    async def download(url, path):
        async with session.get(url) as r:
            content = await r.read()
            content = content.replace(b_root_path, b'')
            if path.endswith(('.html', '.xhtml')):
                title = path.rsplit('/', 1)[-1].rsplit('.', 1)[0]
                content = to_xhtml(content, title)
            zfh.writestr(path, content)

    zfh.writestr('mimetype', b'application/epub+zip', compress_type=zipfile.ZIP_STORED)
    zfh.writestr('META-INF/container.xml', CONTAINER)

    url = BASE_URL + root_path
    while url:
        print(f'fetching {url}')
        async with session.get(url) as r:
            data = await r.json()

        await asyncio.gather(*[
            download(result['url'], f'EPUB/{result["full_path"]}')
            for result in data.get('results', [])
        ])

        url = data.get('next')

    patch_opf(zfh)


def patch_opf(zfh):
    """Remove manifest/spine/guide entries for files that weren't downloaded."""
    names = set(zfh.namelist())
    opf_path = 'EPUB/content.opf'
    if opf_path not in names:
        return

    ET.register_namespace('', 'http://www.idpf.org/2007/opf')
    tree = ET.parse(zfh.open(opf_path))
    root = tree.getroot()
    ns = {'opf': 'http://www.idpf.org/2007/opf'}

    manifest = root.find('opf:manifest', ns)
    spine = root.find('opf:spine', ns)
    guide = root.find('opf:guide', ns)

    missing_ids = set()
    for item in list(manifest):
        href = item.get('href', '')
        if f'EPUB/{href}' not in names:
            missing_ids.add(item.get('id'))
            manifest.remove(item)

    if missing_ids:
        for itemref in list(spine):
            if itemref.get('idref') in missing_ids:
                spine.remove(itemref)
        if guide is not None:
            for ref in list(guide):
                href = ref.get('href', '')
                if f'EPUB/{href}' not in names:
                    guide.remove(ref)

        ET.indent(tree, space='  ')
        from io import BytesIO
        buf = BytesIO()
        tree.write(buf, xml_declaration=True, encoding='UTF-8')
        zfh.writestr(opf_path, buf.getvalue())


def parse_cookies(raw):
    cookies = {}
    for part in raw.split(';'):
        part = part.strip()
        if '=' in part:
            k, _, v = part.partition('=')
            cookies[k.strip()] = v.strip()
    return cookies


async def amain():
    parser = argparse.ArgumentParser()
    parser.add_argument('book_id')
    parser.add_argument('--cookie', '--jwt', dest='cookie')
    args = parser.parse_args()

    filename = f'{args.book_id}.epub'

    raw = args.cookie or ''
    if raw.startswith('eyJ') or '.' in raw[:10]:
        cookies = {'orm-jwt': raw}
    else:
        cookies = {k: v for k, v in parse_cookies(raw).items()
                   if k in ('orm-jwt', 'orm-rt')}

    with zipfile.ZipFile(filename, 'w') as zfh:
        async with aiohttp.ClientSession(
            raise_for_status=True,
            cookies=cookies or None,
        ) as session:
            if not cookies:
                print('No credentials provided. Continuing without…')
            elif await check_auth(session):
                print('Authentication successful.')
            else:
                print('Authentication failed. Continuing without…')

            await fetch_book(args.book_id, zfh, session)

    print(f'created {filename}')


if __name__ == '__main__':
    asyncio.run(amain())
