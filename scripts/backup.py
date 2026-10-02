#!/usr/bin/env python3
"""Backup and restore for the HRMS PostgreSQL database.

The SRS §10 asks for "Postgres: automated daily full backup + WAL archiving,
30-day retention, quarterly restore drill". None of it existed, and that is the
item most expensive to discover late: an untested backup is a belief, not a
control, and the moment you find out it was a belief is the day you need it.

**What this is.** A `pg_dump` custom-format backup with retention, a restore that
can land in a throwaway database, and a **verify** step that restores into a scratch
database and runs the application's own gates against it. The drill is the point:
a backup that has never been restored is not evidence of anything.

**WAL archiving** is deliberately *not* implemented here. Point-in-time recovery
needs a WAL destination (S3, GCS, an archive volume) and a retention policy that
belongs to whoever owns the storage, so a script that pretends to do it would be
worse than one that says so. `verify` therefore checks that a base backup restores,
and `docs/MIGRATION.md` records that PITR is an operator decision with the
requirement to back it.

Usage::

    python scripts/backup.py backup                     # dump + prune to RETENTION_DAYS
    python scripts/backup.py list                       # what exists, and how old
    python scripts/backup.py verify [--file PATH]       # restore into a scratch DB + run the gates
    python scripts/backup.py restore --file PATH --target-db hrms_restored

Connection comes from ``DATABASE_URL``. The DSN is normalised from the
``postgresql+psycopg://`` form the application uses, because ``pg_dump`` wants a
plain ``postgresql://`` and a script that silently produces an empty backup is worse
than one that refuses.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKUP_DIR = pathlib.Path(os.getenv('BACKUP_DIR', str(REPO_ROOT / 'backups')))

#: SRS: 30-day retention.
RETENTION_DAYS = int(os.getenv('BACKUP_RETENTION_DAYS', '30'))

#: Name a backup can be recognised by, and a scratch database can be created from.
_PREFIX = 'hrms-'


def _dsn(database: str | None = None) -> str:
    url = os.getenv('DATABASE_URL')
    if not url:
        sys.exit('DATABASE_URL is not set; there is no sensible default for a backup')
    dsn = url.replace('postgresql+psycopg://', 'postgresql://')
    if database:
        base, _, _ = dsn.rpartition('/')
        if not base:
            sys.exit(f'could not rewrite DATABASE_URL for database {database!r}')
        return f'{base}/{database}'
    return dsn


_CONTAINER = os.getenv('POSTGRES_CONTAINER', 'hrms-portal-postgres-1')


def _have_on_path(binary: str) -> bool:
    import shutil

    return shutil.which(binary) is not None


def _in_container(binary: str) -> bool:
    try:
        return subprocess.run(
            ['docker', 'exec', _CONTAINER, 'which', binary],
            capture_output=True, text=True, timeout=30,
        ).returncode == 0
    except Exception:
        return False


def _tool(binary: str) -> list[str]:
    """How to invoke `binary`: directly if we have it, else through the container.

    A host without the PostgreSQL client tools still needs to be able to take a
    backup — that is the common case for this project's compose setup, where the
    database is a container. The failure mode of *not* handling it is a backup
    directory that has been quietly empty for a month.
    """
    if _have_on_path(binary):
        return [binary]
    if _in_container(binary):
        return ['docker', 'exec', '-i', _CONTAINER, binary]
    sys.exit(
        f'{binary} is not on PATH and not inside {_CONTAINER}. Install the '
        f'PostgreSQL client or set POSTGRES_CONTAINER to the container holding it.'
    )


def _run(cmd: list[str], *, env: dict | None = None, capture: bool = True,
         stdin_file: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    """Run a pg_* tool, transparently through the container when that is where it is.

    `stdin_file` exists because `pg_restore` inside a container cannot see a host
    path: the dump has to be streamed in rather than named.
    """
    if cmd and cmd[0] in ('pg_dump', 'psql', 'pg_restore') and not _have_on_path(cmd[0]):
        full = ['docker', 'exec', '-i', _CONTAINER] + cmd
        stdin = open(stdin_file, 'rb') if stdin_file else None
        try:
            result = subprocess.run(
                full, stdin=stdin, capture_output=capture, text=not stdin, env=env,
                timeout=3600,
            )
        finally:
            if stdin:
                stdin.close()
    else:
        result = subprocess.run(cmd, capture_output=capture, text=True, env=env, timeout=3600)
    if result.returncode != 0:
        sys.exit(f'command failed ({result.returncode}): {" ".join(cmd)}\n{result.stderr}')
    return result


def backups() -> list[pathlib.Path]:
    if not BACKUP_DIR.exists():
        return []
    return sorted(BACKUP_DIR.glob(f'{_PREFIX}*.dump'))


def _stamp() -> str:
    return dt.datetime.now().strftime('%Y%m%dT%H%M%SZ')


def backup() -> int:
    """One full dump, then prune anything past the retention window."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f'{_PREFIX}{_stamp()}.dump'
    dsn = _dsn_for_tool(_dsn())
    env = dict(os.environ, PGPASSWORD=_password(dsn))
    # `pg_dump --file` cannot name a *host* path when it runs inside the container,
    # so in that case the dump is written to stdout and streamed to the file here.
    if _have_on_path('pg_dump'):
        _run(['pg_dump', '--format=custom', '--no-owner', '--no-privileges',
              '--file', str(target), dsn], env=env, capture=False)
    else:
        with open(target, 'wb') as handle:
            result = subprocess.run(
                _tool('pg_dump') + ['--format=custom', '--no-owner', '--no-privileges', dsn],
                stdout=handle, stderr=subprocess.PIPE, env=env, timeout=3600,
            )
        if result.returncode != 0:
            target.unlink(missing_ok=True)
            sys.exit(f'pg_dump failed: {result.stderr.decode()[-500:]}')

    size = target.stat().st_size
    if size == 0:
        sys.exit('pg_dump produced an empty file — treating that as a failure, not a backup')
    print(f'wrote {target} ({size / 1_048_576:.1f} MiB)')

    cutoff = dt.datetime.now() - dt.timedelta(days=RETENTION_DAYS)
    pruned = 0
    for old in backups():
        stamp = old.stem.replace(_PREFIX, '')
        try:
            when = dt.datetime.strptime(stamp, '%Y%m%dT%H%M%SZ')
        except ValueError:
            continue
        if when < cutoff:
            old.unlink()
            pruned += 1
    print(f'retention {RETENTION_DAYS}d: pruned {pruned}, kept {len(backups())}')
    return 0


def _dsn_for_tool(dsn: str) -> str:
    """The DSN as the *tool* must see it.

    When `pg_dump` runs inside the database container, the host's published port is
    meaningless: from in there, `localhost:55432` is the container's own loopback
    and nothing is listening on it. The published port exists on the *host*, not in
    the container, so the DSN is rewritten to the container's internal endpoint —
    which is exactly the "connection refused" you get if this is missed.

    Only the host and port are touched. Credentials, database name and options are
    the caller's, because a backup that silently connected to a *different* database
    than the application uses would be far worse than one that failed.
    """
    if _have_on_path('pg_dump'):
        return dsn
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(dsn)
    # `urlparse` exposes the host, port and credentials together as `netloc`, so
    # splitting it is the only way to rewrite the endpoint without discarding the
    # userinfo. An earlier version of this rebuilt netloc from the host alone and
    # threw the credentials away, which surfaced as `role "root" does not exist` —
    # the kind of failure that looks like a database problem and is not one.
    userinfo, _, hostport = parsed.netloc.rpartition('@')
    host = os.getenv('POSTGRES_INTERNAL_HOST', 'localhost')
    port = os.getenv('POSTGRES_INTERNAL_PORT', '5432')
    netloc = f'{userinfo}@{host}:{port}' if userinfo else f'{host}:{port}'
    return urlunparse((
        parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment,
    ))


def _password(dsn: str) -> str:
    """Pull the password out of the DSN for PGPASSWORD."""
    from urllib.parse import unquote, urlparse

    parsed = urlparse(dsn)
    return unquote(parsed.password or '')


def list_backups() -> int:
    items = backups()
    if not items:
        print(f'no backups in {BACKUP_DIR} — the daily backup has not run, or is writing elsewhere')
        return 1
    now = dt.datetime.now()
    for item in items:
        age = now - dt.datetime.fromtimestamp(item.stat().st_mtime)
        flag = '  <-- OLDER THAN RETENTION' if age.days > RETENTION_DAYS else ''
        print(f'{item.name}  {item.stat().st_size / 1_048_576:8.1f} MiB  '
              f'{age.days:3d} days old{flag}')
    return 0


def restore(file: pathlib.Path, target_db: str) -> None:
    """Recreate `target_db` and load `file` into it."""
    admin = _dsn_for_tool(_dsn('postgres'))
    env = dict(os.environ, PGPASSWORD=_password(admin))
    _run(['psql', admin, '-c', f'DROP DATABASE IF EXISTS {target_db}'], env=env, capture=False)
    _run(['psql', admin, '-c', f'CREATE DATABASE {target_db}'], env=env, capture=False)
    if _have_on_path('pg_restore'):
        _run(['pg_restore', '--no-owner', '--no-privileges', '--dbname', _dsn(target_db),
              str(file)], env=env, capture=False)  # local tools see the host DSN
    else:
        # The dump is a host path the container cannot open, so it is streamed in
        # on stdin instead. This is the difference between a restore drill that runs
        # on a normal development host and one that only works where the client
        # happens to be installed.
        _run(['pg_restore', '--no-owner', '--no-privileges', '--dbname',
              _dsn_for_tool(_dsn(target_db))], env=env, stdin_file=file)
    print(f'restored {file.name} into {target_db}')


def verify(file: pathlib.Path | None = None) -> int:
    """The quarterly drill: restore, then run the application's own gates.

    Restoring is the easy half. What makes this a *drill* is that the restored
    database is then held to the same standard as the live one — schema at the
    expected Alembic head, identity keys present, sequences ahead of data — because
    a backup that restores into a database the application would refuse to run is
    not a backup.
    """
    if file is None:
        candidates = backups()
        if not candidates:
            sys.exit(f'no backups to verify in {BACKUP_DIR}')
        file = candidates[-1]
    if not file.exists():
        sys.exit(f'{file} does not exist')

    scratch = os.getenv('RESTORE_DRILL_DB', 'hrms_restore_drill')
    # **Two DSNs, on purpose.** `psql`/`pg_dump` may be executing inside the
    # database container, where the host's published port does not exist and the
    # endpoint must be rewritten. The application's gates are Python running on
    # *this* host, where the published port is exactly right. Handing the tool DSN
    # to a Python gate fails with a connection error that reads like a broken
    # backup rather than a wrong endpoint, which is a genuinely confusing hour.
    host_dsn = _dsn(scratch)
    tool_dsn = _dsn_for_tool(host_dsn)
    print(f'== restore drill on {file.name} into {scratch} ==')
    restore(file, scratch)

    failures: list[str] = []

    # 1. The application's own gates, pointed at the restored copy.
    for label, cmd in (
        ('CC-01 identity keys', [sys.executable, str(REPO_ROOT / 'scripts' / 'check_cc_rules.py')]),
        ('cutover preflight', [sys.executable, str(REPO_ROOT / 'scripts' / 'cutover_preflight.py'),
                               '--schema', 'public']),
    ):
        env = dict(os.environ, DATABASE_URL=host_dsn)
        result = subprocess.run(cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True,
                                timeout=1800)
        ok = result.returncode == 0
        print(f'  {"PASS" if ok else "FAIL"}  {label}')
        if not ok:
            failures.append(f'{label}: {result.stderr.strip()[-400:]}')

    # 2. A row count the operator can eyeball against the source. A backup that
    #    restores an empty database passes every gate above, because an empty
    #    database is schema-valid.
    # Tolerate a dump that has no `users` table at all. Without this the check
    # crashes on a raw psql error, which reads like a tooling problem rather than
    # the backup failure it actually is — and it takes the report down with it.
    env = dict(os.environ, PGPASSWORD=_password(tool_dsn))
    has_table = _run(
        ['psql', tool_dsn, '-tAc',
         "SELECT to_regclass('users') IS NOT NULL"], env=env,
    ).stdout.strip()
    if has_table != 't':
        print('  FAIL  restored database has no users table at all')
        failures.append(
            'the restored dump contains no `users` table. Either the backup is of '
            'the wrong database, or it was taken before the application ever ran.'
        )
    else:
        counts = _run(['psql', tool_dsn, '-tAc',
                       "SELECT 'users=' || count(*) FROM users"], env=env).stdout.strip()
        print(f'  INFO  restored {counts}')
        if counts.endswith('=0'):
            failures.append(
                f'restored database has no users ({counts}). Every gate above still '
                f'passes on an empty database, which is exactly why this check exists.'
            )

    admin = _dsn_for_tool(_dsn('postgres'))
    env = dict(os.environ, PGPASSWORD=_password(admin))
    _run(['psql', admin, '-c', f'DROP DATABASE IF EXISTS {scratch}'],
         env=env, capture=False)

    if failures:
        print('\nRESTORE DRILL FAILED')
        for f in failures:
            print(f'  - {f}')
        return 1
    print('\nRESTORE DRILL PASSED')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('backup', help='take a full dump and prune to the retention window')
    sub.add_parser('list', help='list backups with age and retention status')
    v = sub.add_parser('verify', help='restore the newest backup to a scratch DB and run the gates')
    v.add_argument('--file', type=pathlib.Path, help='verify a specific dump instead of the newest')
    r = sub.add_parser('restore', help='restore a dump into a named database')
    r.add_argument('--file', required=True, type=pathlib.Path)
    r.add_argument('--target-db', required=True)
    args = parser.parse_args()

    if args.command == 'backup':
        return backup()
    if args.command == 'list':
        return list_backups()
    if args.command == 'verify':
        return verify(args.file)
    if args.command == 'restore':
        restore(args.file, args.target_db)
        return 0
    return 1


if __name__ == '__main__':
    sys.exit(main())
