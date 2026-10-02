#!/usr/bin/env python3
"""Measure the SRS §10 latency and error-rate targets against a running server.

The k6 scenario in ``ops/load/load.js`` is the deliverable the SRS names, and it is
what should run against staging. This is the version that can run **here and now**,
so the targets are measured rather than asserted:

* API latency **p95 < 300 ms, p99 < 800 ms** (Performance NFR)
* error rate **< 0.1%** (Concurrency NFR)

It is deliberately small and dependency-free — threads plus ``urllib`` — because a
load harness that needs its own toolchain is a load harness that does not get run,
and an unrun harness measures nothing. It is a *smoke* measurement: enough to catch
an order-of-magnitude regression and to give the SRS numbers an observed value, and
explicitly **not** a substitute for 150 req/s for 15 minutes in staging.

Usage::

    python scripts/loadcheck.py --base-url http://127.0.0.1:5000 --rps 40 --seconds 20
    python scripts/loadcheck.py --base-url http://127.0.0.1:5000 --read-only

Exit code is 0 when every measured target is met and 1 when any is missed, so it can
gate a build. "Missed" is reported as *missed*, never silently rounded away: a
latency target that is not met is the finding.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

# SRS §10 Performance NFR.
P95_TARGET_MS = 300.0
P99_TARGET_MS = 800.0
ERROR_RATE_TARGET = 0.001

#: The read surface a signed-in user actually hits. Payroll and the export routes
#: are excluded because the SRS excludes async-job endpoints from the latency
#: target, and including a report generation would dominate the percentile with
#: something the requirement deliberately leaves out.
#:
#: **Every one of these is reachable by an ordinary Employee.** An earlier version
#: included `/api/dashboard-stats`, which is admin-only, so a 17% "error rate" was
#: the harness asking for something the account may not have and counting the
#: correct 403 as a server fault. A load harness that measures authorisation failures
#: while reporting them as latency errors is worse than no harness, because the
#: number looks like a finding about the server.
READ_ENDPOINTS = (
    '/api/profile',
    '/api/user-breaks',
    '/api/notifications',
    '/api/leave-balance',
    '/api/user/calendar',
    '/api/my-assets',
    '/api/goals',
)


class Results:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.latencies: list[float] = []
        self.errors = 0
        self.statuses: dict[int, int] = {}
        self.lockouts = 0
        self.denied = 0

    def record(self, ms: float, status: int | None) -> None:
        with self.lock:
            self.latencies.append(ms)
            # 401/403 is the authorisation layer answering correctly, not the server
            # failing. Counting them as errors made a correctly-locked-down API look
            # 17% broken. They are counted separately and reported, because "the
            # load account cannot read this endpoint" is itself worth knowing.
            if status is None or (status >= 400 and status not in (401, 403)):
                self.errors += 1
            elif status in (401, 403):
                self.denied += 1
            if status is not None:
                self.statuses[status] = self.statuses.get(status, 0) + 1
            if status == 429:
                self.lockouts += 1

    def percentile(self, pct: float) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        # Nearest-rank, which is what k6 reports, rather than an interpolation that
        # would report a number the server never produced.
        rank = max(1, int(round(pct / 100.0 * len(ordered))))
        return ordered[min(rank, len(ordered)) - 1]


def _request(url: str, headers: dict[str, str] | None = None,
             data: bytes | None = None) -> tuple[int | None, float]:
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
            return resp.status, (time.perf_counter() - started) * 1000.0
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code, (time.perf_counter() - started) * 1000.0
    except Exception:
        return None, (time.perf_counter() - started) * 1000.0


def login(base: str, emp_id: str, password: str) -> tuple[str, dict[str, str]]:
    status, _ = _request(f'{base}/api/csrf-token')
    if status != 200:
        return '', {}
    import http.cookiejar

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    # The CSRF token has to come from the same session that will make the write.
    with opener.open(f'{base}/api/csrf-token', timeout=30) as resp:
        token = json.loads(resp.read()).get('csrf_token')
    body = json.dumps({'emp_id': emp_id, 'password': password}).encode()
    req = urllib.request.Request(
        f'{base}/login', data=body,
        headers={'Content-Type': 'application/json', 'X-CSRF-Token': token or ''},
    )
    try:
        with opener.open(req, timeout=30) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read() or b'{}')
        if payload.get('mfa_required'):
            sys.exit(
                f'{emp_id} requires a second factor, and this harness cannot complete a '
                f'TOTP challenge. Point it at an account whose role is not mandatory '
                f'(Employee is fine) — a run that measures only the 401 path proves '
                f'nothing about latency.'
            )
        raise SystemExit(f'login as {emp_id} failed: {payload}')
    cookie = '; '.join(f'{c.name}={c.value}' for c in jar)
    if payload.get('mfa_required'):
        sys.exit(f'{emp_id} requires a second factor; use a non-mandatory role.')
    return cookie, {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base-url', default='http://127.0.0.1:5000')
    parser.add_argument('--emp-id', default='EMP002')
    parser.add_argument('--password', default='pass123')
    parser.add_argument('--rps', type=int, default=40)
    parser.add_argument('--seconds', type=int, default=20)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--read-only', action='store_true',
                        help='measure unauthenticated endpoints only')
    args = parser.parse_args()

    base = args.base_url.rstrip('/')
    cookie = ''
    if not args.read_only:
        cookie, _ = login(base, args.emp_id, args.password)

    results = Results()
    endpoints = READ_ENDPOINTS if cookie else ('/api/health', '/login')
    total_requests = args.rps * args.seconds
    per_worker = max(1, total_requests // args.workers)
    interval = 1.0 / max(1, args.rps // args.workers)
    stop = time.time() + args.seconds

    def worker(slot: int) -> None:
        i = 0
        while time.time() < stop and i < per_worker:
            endpoint = endpoints[i % len(endpoints)]
            status, ms = _request(
                f'{base}{endpoint}',
                {'Cookie': cookie} if cookie else {},
            )
            results.record(ms, status)
            i += 1
            if interval:
                time.sleep(interval)

    threads = [threading.Thread(target=worker, args=(n,), daemon=True)
               for n in range(args.workers)]
    started = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=args.seconds + 30)
    elapsed = time.time() - started

    made = len(results.latencies)
    error_rate = results.errors / made if made else 1.0
    p95 = results.percentile(95)
    p99 = results.percentile(99)

    print(f'\n{base}  {args.rps} req/s x {args.seconds}s  ->  '
          f'{made} requests in {elapsed:.1f}s ({made / max(elapsed, 0.001):.0f} req/s actual)')
    if results.statuses:
        print('  statuses: ' + ', '.join(
            f'{code}={n}' for code, n in sorted(results.statuses.items())))
    if results.denied:
        print(f'  {results.denied} request(s) answered 401/403 — the authorisation layer '
              f'working, not counted as errors. If this is non-zero the load account\'s '
              f'role cannot read every endpoint being measured; fix the harness or the '
              f'account, do not treat it as a latency finding.')
    if made:
        print(f'  median   {statistics.median(results.latencies):7.1f} ms')

    print('\n== SRS §10 targets ==')
    rows = [
        ('p95 latency', p95, f'< {P95_TARGET_MS:.0f} ms', p95 < P95_TARGET_MS),
        ('p99 latency', p99, f'< {P99_TARGET_MS:.0f} ms', p99 < P99_TARGET_MS),
        ('error rate', error_rate * 100, '< 0.1 %', error_rate < ERROR_RATE_TARGET),
        ('shared-NAT lockouts', results.lockouts, '0', results.lockouts == 0),
    ]
    ok = True
    for label, value, target, passed in rows:
        unit = '%' if label == 'error rate' else ('ms' if 'ms' in target else '')
        shown = f'{value:.3f}{unit}' if unit == '%' else f'{value:.1f}{unit}'
        print(f'  {"PASS" if passed else "FAIL"}  {label:20} {shown:>12}  target {target}')
        ok = ok and passed

    if not ok:
        print('\nOne or more SRS §10 targets were not met. This harness is a smoke')
        print('measurement; run ops/load/load.js against staging for the real figures.')
    print('')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
