# /// script
# dependencies = [
#   "aiohttp",
# ]
# ///

import argparse
import asyncio
import zipfile

import aiohttp

BASE_URL = 'https://learning.oreilly.com'

CONTAINER = b"""<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
    <rootfiles>
        <rootfile full-path="EPUB/content.opf" media-type="application/oebps-package+xml"/>
    </rootfiles>
</container>
"""  # noqa


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
            content = content.replace(b_root_path, b'/EPUB/')
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
