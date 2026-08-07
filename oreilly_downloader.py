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


def to_xhtml(s, root_path):
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

        h1 = tree.find('.//h1')
        if h1 is not None:
            head = etree.SubElement(wrapper, 'head')
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


async def fetch_book(book_id, session):
    filename = f'{book_id}.epub'

    with zipfile.ZipFile(filename, 'w') as zfh:
        root_path = f'/api/v2/epubs/urn:orm:book:{book_id}/files/'

        async def download(url, path):
            async with session.get(url) as r:
                content = await r.read()
                if path.endswith('.html'):
                    content = to_xhtml(content, root_path)
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


async def fetch_collections(session):
    url = BASE_URL + '/api/v3/collections/'

    async with session.get(url, raise_for_status=False) as r:
        if r.status != 200:
            return None

        return await r.json()


async def fetch_collection(collection_id, session):
    collections = await fetch_collections(session)
    
    if collections is None:
        return None

    collection = next(c for c in collections if c.get('id') == collection_id)
    for book in collection.get('content'):
        await fetch_book(book.get('api_url').split('/')[-2], session)


async def fetch_all(session):
    collections = await fetch_collections(session)

    if collections is None:
        print("Nothing to download.")
        return

    fetched_ids = set()
    
    for collection in collections:
        for book in collection.get('content'):
            book_id = book.get('api_url').split('/')[-2]

            if book_id not in fetched_ids:
                await fetch_book(book_id, session)

            fetched_ids.add(book.get('api_url').split('/')[-2])


async def amain():
    parser = argparse.ArgumentParser()

    target_options = parser.add_mutually_exclusive_group(required=True)
    
    target_options.add_argument('--all', action='store_true')
    target_options.add_argument('--collection', type=str)
    target_options.add_argument('--book', type=str)
    parser.add_argument('--jwt')

    args = parser.parse_args()

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

        if args.all is True:
            await fetch_all(session)
        elif args.collection is not None:
            await fetch_collection(args.collection, session)
        elif args.book is not None:
            await fetch_book(args.book, session)


if __name__ == '__main__':
    asyncio.run(amain())
