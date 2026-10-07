# /// script
# dependencies = [
#   "aiohttp",
#   "lxml",
# ]
# ///

import argparse
import asyncio
import zipfile

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


def to_xhtml(s, root_path, stylesheets):
    tree = lhtml.fromstring(s, parser=lhtml.HTMLParser(encoding='utf-8'))

    for el in list(tree.iter()):
        for attr in ['href', 'src']:
            if (el.get(attr) or '').startswith(root_path):
                el.set(attr, el.get(attr).removeprefix(root_path))

    if tree.tag != 'html':
        wrapper = etree.Element('html', nsmap={
            None: 'http://www.w3.org/1999/xhtml',
            'epub': 'http://www.idpf.org/2007/ops',
        })

        head = etree.SubElement(wrapper, 'head')
        for href in stylesheets:
            etree.SubElement(head, 'link', rel='stylesheet', href=href)
        h1 = tree.find('.//h1')
        if h1 is not None:
            title = etree.SubElement(head, 'title')
            title.text = ''.join(h1.itertext()).strip()

        body = etree.SubElement(wrapper, 'body')
        body.append(tree)
        tree = wrapper

    return etree.tostring(
        tree,
        xml_declaration=True,
        doctype='<!DOCTYPE html>',
        pretty_print=True,
        encoding='utf-8',
    )


async def check_auth(session):
    url = BASE_URL + '/api/v1/user-preferences/'
    async with session.get(url, raise_for_status=False) as r:
        return r.ok


async def fetch_book(book_id, zfh, session, *, delay=0):
    root_path = f'/api/v2/epubs/urn:orm:book:{book_id}/files/'
    html = {}

    async def download(url, path):
        async with session.get(url) as r:
            content = await r.read()
            if path.endswith(('.html', '.xhtml')):
                html[path] = content
            else:
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
        if url:
            await asyncio.sleep(delay)

    css = [p.removeprefix('EPUB/') for p in zfh.namelist() if p.endswith('.css')]
    for path, content in html.items():
        zfh.writestr(path, to_xhtml(content, root_path, css))


async def amain():
    parser = argparse.ArgumentParser()
    parser.add_argument('book_id')
    parser.add_argument('--jwt')
    parser.add_argument('--delay', type=int, default=0, help=(
        'Seconds to wait between batches of file downloads. '
        'Workaround for 403 errors caused by rate-limiting.'
    ))
    args = parser.parse_args()

    filename = f'{args.book_id}.epub'

    with zipfile.ZipFile(filename, 'w') as zfh:
        async with aiohttp.ClientSession(
            raise_for_status=True,
            cookies={'orm-jwt': args.jwt},
        ) as session:
            if not args.jwt:
                print('No JWT provided. Continuing without…')
            elif await check_auth(session):
                print('Authentication successful.')
            else:
                print('Authentication failed. Continuing without…')

            await fetch_book(args.book_id, zfh, session, delay=args.delay)

    print(f'created {filename}')


if __name__ == '__main__':
    asyncio.run(amain())
