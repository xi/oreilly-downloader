# O'Reilly epub downloader

O'Reilly provides all of their books in epub format, but only through their own
reader.

This script allows you to download all the individual files and assemble them
back into a full epub. This allows you to use other readers, e.g. for
accessibility reasons.

You need valid authentication cookies to download content. If you do not
provide them, each chapter will be cut short. Log in with your browser, then
copy the whole `Cookie` request header (developer tools → Network) and pass it
to `--cookie`. Both the `orm-jwt` and `orm-rt` cookies are required for
authentication to succeed.

Before any usage, please read the [O'Reilly Terms of
Service](https://learning.oreilly.com/terms/).

# Usage

```
$ python3 oreilly_downloader.py 9781491958698 --cookie 'orm-jwt=…; orm-rt=…'
…
created 9781491958698.epub
```

The script declares its dependencies inline ([PEP 723](https://peps.python.org/pep-0723/)),
so [`uv`](https://docs.astral.sh/uv/) can run it without a manual install:

```
$ uv run oreilly_downloader.py 9781491958698 --cookie 'orm-jwt=…; orm-rt=…'
```

# Similar Projects

-   <https://github.com/lorenzodifuccia/safaribooks> (python)
-   <https://github.com/hurlenko/orly> (rust)
-   <https://github.com/jenni/obooks> (javascript)
-   <https://github.com/rahulvramesh/oreilly-books-grabber> (go)
