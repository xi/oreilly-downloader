# O'Reilly epub downloader

O'Reilly provides all of their books in epub format, but only through their own
reader.

This script allows you to download all the individual files and assemble them
back into a full epub. This allows you to use other readers, e.g. for
accessibility reasons.

You need to have a valid JWT to download content. If you do not provide one,
each chapter will be cut short. You can get it by logging in with your browser
and extracting the `orm-jwt` cookie using the developer tools.

Before any usage, please read the [O'Reilly Terms of
Service](https://learning.oreilly.com/terms/).

# Usage


## Download a single book

```
$ pip install aiohttp
$ python3 oreilly_downloader.py --book 9781491958698 --jwt 'XYZ'
...
created 9781491958698.epub
```

## Download a playlist/collection 

```
$ pip install aiohttp
$ python3 oreilly_downloader.py --collection <collection-id> --jwt 'XYZ'
...
created 9781491958698.epub
created 9781491958699.epub
```

## Download all saved playlists/collections

```
$ pip install aiohttp
$ python3 oreilly_downloader.py --all --jwt 'XYZ'
...
created 9781491958698.epub
created 9781491958699.epub
created 9781491958700.epub
created 9781491958701.epub
created 9781491958702.epub
```

# Similar Projects

-   <https://github.com/lorenzodifuccia/safaribooks> (python)
-   <https://github.com/hurlenko/orly> (rust)
-   <https://github.com/jenni/obooks> (javascript)
-   <https://github.com/rahulvramesh/oreilly-books-grabber> (go)
