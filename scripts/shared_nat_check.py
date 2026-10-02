#!/usr/bin/env python3
"""Shared-NAT check: N distinct employees behind ONE egress address.

This is the scenario that made the shipped rate limits a launch blocker, and it is
not something a single-account harness can express. Every user here is a different
person with their own account, their own session and their own legitimate traffic
— and they all arrive from `127.0.0.1`, which is exactly what a corporate NAT, a
mobile carrier or a cloud egress does.

Under the old configuration (`key_func=get_remote_address`, 200/minute) the *first*
user's browsing exhausted a single shared bucket and everyone behind that address
was locked out. The SRS's own burst NFR forbids this in as many words: "without
lockouts caused by shared-NAT rate limiting (per-account, not per-IP-only)".

Run against a live server::

    python scripts/shared_nat_check.py --base-url http://127.0.0.1:5000 --users 15
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import threading
import urllib.error
import urllib.request


def login(base: str, emp_id: str, password: str) -> tuple[str | None, int]:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f'{base}/api/csrf-token', timeout=30) as resp:
        token = json.loads(resp.read()).get('csrf_token')
    body = json.dumps({'emp_id': emp_id, 'password': password}).encode()
    req = urllib.request.Request(
        f'{base}/login', data=body,
        headers={'Content-Type': 'application/json', 'X-CSRF-Token': token or ''},
    )
    try:
        with opener.open(req, timeout=30) as resp:
            resp.read()
            return '; '.join(f'{c.name}={c.value}' for c in jar), 200
    except urllib.error.HTTPError as exc:
        exc.read()
        return None, exc.code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base-url', default='http://127.0.0.1:5000')
    parser.add_argument('--first-emp', default='EMP961')
    parser.add_argument('--users', type=int, default=15)
    parser.add_argument('--password', default='pass123')
    parser.add_argument('--requests-per-user', type=int, default=25)
    args = parser.parse_args()
    base = args.base_url.rstrip('/')

    lock = threading.Lock()
    denied: list[tuple[str, int]] = []
    served: dict[str, int] = {}

    def drive(offset: int) -> None:
        emp = f'EMP{961 + offset}'
        cookie, status = login(base, emp, args.password)
        if cookie is None:
            with lock:
                denied.append((emp, status))
            return
        ok = 0
        for _ in range(args.requests_per_user):
            req = urllib.request.Request(f'{base}/api/profile', headers={'Cookie': cookie})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp.read()
                    ok += 1
            except urllib.error.HTTPError as exc:
                exc.read()
                with lock:
                    denied.append((emp, exc.code))
        with lock:
            served[emp] = ok

    threads = [threading.Thread(target=drive, args=(n,), daemon=True)
               for n in range(args.users)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)

    total = args.users * args.requests_per_user
    got = sum(served.values())
    print(f'\n{args.users} distinct employees, ONE egress address, '
          f'{args.requests_per_user} reads each')
    print(f'  served {got}/{total}   locked out {len(denied)}')
    if denied:
        codes: dict[int, int] = {}
        for _emp, code in denied:
            codes[code] = codes.get(code, 0) + 1
        print('  denial statuses: ' + ', '.join(f'{c}x{n}' for c, n in sorted(codes.items())))
        print('\nFAIL — employees behind a shared address are being locked out by each')
        print('       other. The SRS burst NFR requires per-account limiting.')
        return 1
    if served and len(served) != args.users:
        print(f'\nFAIL — only {len(served)}/{args.users} employees were served at all')
        return 1
    print('\nPASS — every employee was served independently behind the same address')
    return 0


if __name__ == '__main__':
    sys.exit(main())
