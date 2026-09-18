Successfully installed aiofiles-25.1.0 aiogram-3.31.0 aiohappyeyeballs-2.7.1 aiohttp-3.14.3 aiosignal-1.4.0 annotated-types-0.8.0 apscheduler-3.11.3 asyncpg-0.31.0 attrs-26.1.0 certifi-2026.7.22 frozenlist-1.8.0 idna-3.20 magic-filter-1.0.12 multidict-6.8.0 propcache-0.5.4 pydantic-2.13.5 pydantic-core-2.46.5 typing-extensions-4.16.0 typing-inspection-0.4.4 tzdata-2026.4 tzlocal-5.4.4 yarl-1.25.1
[notice] A new release of pip is available: 25.3 -> 26.2.1
[notice] To update, run: pip install --upgrade pip
==> Uploading build...
==> Uploaded in 2.1s. Compression took 1.8s
==> Build successful 🎉
==> Deploying...
==> Setting WEB_CONCURRENCY=1 by default, based on available CPUs in the instance
==> Running 'python bot.py'
Traceback (most recent call last):
  File "/opt/render/project/src/bot.py", line 703, in <module>
    asyncio.run(main())
    ~~~~~~~~~~~^^^^^^^^
  File "/opt/render/project/python/Python-3.14.3/lib/python3.14/asyncio/runners.py", line 204, in run
    return runner.run(main)
           ~~~~~~~~~~^^^^^^
  File "/opt/render/project/python/Python-3.14.3/lib/python3.14/asyncio/runners.py", line 127, in run
    return self._loop.run_until_complete(task)
           ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^
  File "/opt/render/project/python/Python-3.14.3/lib/python3.14/asyncio/base_events.py", line 719, in run_until_complete
    return future.result()
           ~~~~~~~~~~~~~^^
  File "/opt/render/project/src/bot.py", line 676, in main
    db_pool = await asyncpg.create_pool(dsn=DB_URI)
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/asyncpg/pool.py", line 439, in _async__init__
    await self._initialize()
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/asyncpg/pool.py", line 466, in _initialize
    await first_ch.connect()
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/asyncpg/pool.py", line 153, in connect
    self._con = await self._pool._get_new_connection()
                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/asyncpg/pool.py", line 538, in _get_new_connection
    con = await self._connect(
          ^^^^^^^^^^^^^^^^^^^^
    ...<5 lines>...
    )
    ^
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/asyncpg/connection.py", line 2443, in connect
    return await connect_utils._connect(
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ...<22 lines>...
    )
    ^
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/asyncpg/connect_utils.py", line 1209, in _connect
    addrs, params, config = _parse_connect_arguments(**kwargs)
                            ~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/asyncpg/connect_utils.py", line 893, in _parse_connect_arguments
    addrs, params = _parse_connect_dsn_and_args(
                    ~~~~~~~~~~~~~~~~~~~~~~~~~~~^
        dsn=dsn, host=host, port=port, user=user,
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ...<4 lines>...
        krbsrvname=krbsrvname, gsslib=gsslib,
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
        service=service, servicefile=servicefile)
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/asyncpg/connect_utils.py", line 289, in _parse_connect_dsn_and_args
    parsed = urllib.parse.urlparse(dsn)
  File "/opt/render/project/python/Python-3.14.3/lib/python3.14/urllib/parse.py", line 395, in urlparse
    scheme, netloc, url, params, query, fragment = _urlparse(url, scheme, allow_fragments)
                                                   ~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/opt/render/project/python/Python-3.14.3/lib/python3.14/urllib/parse.py", line 400, in _urlparse
    scheme, netloc, url, query, fragment = _urlsplit(url, scheme, allow_fragments)
                                           ~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/opt/render/project/python/Python-3.14.3/lib/python3.14/urllib/parse.py", line 523, in _urlsplit
    raise ValueError("Invalid IPv6 URL")
