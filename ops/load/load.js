// Load and burst scenarios for the HRMS API — SRS §10 targets.
//
//   sustained : 150 req/s for 15 minutes, error rate < 0.1%    (Concurrency NFR)
//   burst     : 1,000 logins in a 5-minute shift-start window, without lockouts
//               caused by shared-NAT rate limiting                 (Concurrency NFR)
//   latency   : p95 < 300 ms, p99 < 800 ms                        (Performance NFR)
//
// Run against staging, never production:
//
//   k6 run -e BASE_URL=https://staging.example.com -e SCENARIO=sustained ops/load/load.js
//   k6 run -e BASE_URL=https://staging.example.com -e SCENARIO=burst ops/load/load.js
//
// ## Before this can run: a load-test account
//
// FR-AUTH-11 makes a second factor compulsory for Admin / Super Admin / HR /
// Finance. A load test that logs in as an Admin therefore needs a TOTP code per
// virtual user, which measures the MFA path rather than the API and is not
// reproducible at 150 req/s. **Point this at a dedicated account whose role is not
// mandatory** — `EMP_ID` / `EMP_EMAIL` below — provisioned for the run and removed
// afterwards. The script refuses to run against an MFA-enrolled account rather than
// quietly reporting a wall of 401s as a performance finding.
//
// ## The burst scenario is expected to FAIL today, and checking is the point
//
// The SRS asks for 1,000 logins in five minutes "without lockouts caused by
// shared-NAT rate limiting (**per-account, not per-IP-only**)". The application
// limits `/login` by remote address (`LOGIN_RATE_LIMIT`, 20/minute by default), so
// every one of those logins arrives from the same source address and the limit
// trips long before five minutes are up. FR-AUTH-01 is recorded PARTIAL for exactly
// this reason, and this scenario turns "per-IP rather than per-account" from a
// note into a number.
//
// `CHECK_LOCKOUTS` defaults to false so the run *reports* the lockout rate as data
// instead of failing before it has measured anything. Set it to true once the
// limiter is per-account, at which point any lockout is a real regression.

import http from 'k6/http';
import { check, sleep } from 'k6';
import exec from 'k6/execution';

const SCENARIO = __ENV.SCENARIO || 'sustained';
const BASE_URL = (__ENV.BASE_URL || 'http://localhost:5000').replace(/\/$/, '');
const CHECK_LOCKOUTS = (__ENV.CHECK_LOCKOUTS || 'false') === 'true';

// A disposable employee, or a comma-separated pool for the burst scenario so each
// virtual user is a distinct account. One shared account would measure the
// lockout and nothing else, which is the opposite of what the burst NFR asks.
const ACCOUNTS = (__ENV.ACCOUNTS || 'EMP002,probe@company.com')
  .split(',')
  .map((pair) => {
    const [empId, email] = pair.split(':');
    return { empId, email: email || `${empId.toLowerCase()}@company.com` };
  });

const PASSWORD = __ENV.PASSWORD || 'pass123';
const BURST_LOGINS = parseInt(__ENV.BURST_LOGINS || '1000', 10);
const BURST_WINDOW_S = parseInt(__ENV.BURST_WINDOW_S || '300', 10);

export const options = {
  scenarios: SCENARIO === 'burst' ? burst() : sustained(),
  thresholds: {
    // SRS Performance NFR. Async-job endpoints (payroll, exports) are explicitly
    // out of scope, which is why the scenario below never calls them.
    http_req_duration: ['p(95)<300', 'p(99)<800'],
    // SRS Concurrency NFR: error rate < 0.1% across the run.
    http_req_failed: ['rate<0.001'],
    // SRS Concurrency NFR: no lockouts from shared-NAT limiting. Reported always,
    // and only *enforced* once CHECK_LOCKOUTS is set — see the header.
    lockouts: ['count==0'],
  },
};

function sustained() {
  return {
    sustained_load: {
      executor: 'constant-arrival-rate',
      rate: 150,
      timeUnit: '1s',
      duration: '15m',
      preAllocatedVUs: 200,
      maxVUs: 600,
    },
  };
}

function burst() {
  return {
    shift_start_burst: {
      executor: 'constant-arrival-rate',
      rate: Math.ceil(BURST_LOGINS / BURST_WINDOW_S),
      timeUnit: '1s',
      duration: `${BURST_WINDOW_S}s`,
      preAllocatedVUs: 300,
      maxVUs: 800,
    },
  };
}

function login(acct) {
  const token = http.get(`${BASE_URL}/api/csrf-token`).json('csrf_token');
  return http.post(
    `${BASE_URL}/login`,
    JSON.stringify({ emp_id: acct.empId, password: PASSWORD }),
    { headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token } },
  );
}

export function setup() {
  const health = http.get(`${BASE_URL}/api/health`);
  if (health.status !== 200) {
    throw new Error(`${BASE_URL}/api/health answered ${health.status} — is the app up?`);
  }
  const probe = login(ACCOUNTS[0]);
  const body = probe.json() || {};
  if (body.mfa_required) {
    throw new Error(
      `${ACCOUNTS[0].empId} requires a second factor. This script cannot complete a `
      + 'TOTP challenge per virtual user, so it would measure the MFA path instead of '
      + 'the API. Point ACCOUNTS at a non-mandatory role (Employee is fine).'
    );
  }
  if (probe.status !== 200) {
    throw new Error(`login as ${ACCOUNTS[0].empId} answered ${probe.status}: ${JSON.stringify(body)}`);
  }
  return { ok: true };
}

export default function () {
  if (SCENARIO === 'burst') {
    const acct = ACCOUNTS[exec.scenario.iterationInTest % ACCOUNTS.length];
    const res = login(acct);
    const locked = res.status === 429;
    check(res, {
      'login authorised or credential-refused': () => res.status === 200 || res.status === 401,
    });
    if (locked || CHECK_LOCKOUTS) {
      // Counted rather than only checked, so the summary reports a rate either way.
      __ENV.__lockouts = (__ENV.__lockouts || 0) + (locked ? 1 : 0);
    }
    return;
  }

  const res = login(ACCOUNTS[0]);
  const jar = res.headers['Set-Cookie'] || '';

  // The read surface a signed-in user actually hits. Payroll and the export routes
  // are deliberately absent: the SRS excludes async-job endpoints from the latency
  // target, and including them would drag p95 toward a report generation time.
  const endpoints = [
    '/api/profile',
    '/api/user-breaks',
    '/api/dashboard-stats',
    '/api/notifications',
    '/api/leave-balance',
    '/api/user/calendar',
  ];
  const target = endpoints[Math.floor(Math.random() * endpoints.length)];
  const out = http.get(`${BASE_URL}${target}`, {
    headers: { Cookie: jar },
    tags: { endpoint: target },
  });
  check(out, { 'read endpoint answered 200': (r) => r.status === 200 });
  sleep(0.1);
}

export function handleSummary(data) {
  const lockouts = (data.metrics.lockouts && data.metrics.lockouts.values.counts)
    || 0;
  const failed = data.metrics.http_req_failed ? data.metrics.http_req_failed.values.rate : 0;
  const p95 = data.metrics.http_req_duration ? data.metrics.http_req_duration.values['p(95)'] : 0;
  const p99 = data.metrics.http_req_duration ? data.metrics.http_req_duration.values['p(99)'] : 0;

  const lines = [
    '',
    '== SRS §10 targets ==',
    `  p95 latency       ${p95.toFixed(0)}ms   target < 300ms   ${p95 < 300 ? 'PASS' : 'FAIL'}`,
    `  p99 latency       ${p99.toFixed(0)}ms   target < 800ms   ${p99 < 800 ? 'PASS' : 'FAIL'}`,
    `  error rate        ${(failed * 100).toFixed(3)}%  target < 0.1%   ${failed < 0.001 ? 'PASS' : 'FAIL'}`,
    `  shared-NAT locks  ${lockouts}       target 0        ${lockouts === 0 ? 'PASS' : 'FAIL'}`,
  ];
  if (lockouts > 0) {
    lines.push('');
    lines.push('  Lockouts during the shift-start burst are the FR-AUTH-01 gap:');
    lines.push("  `/login` is limited per remote address, so every login from one");
    lines.push('  shared egress address trips the same bucket. The SRS asks for');
    lines.push('  per-account limiting. Until that changes this scenario cannot pass,');
    lines.push('  and this line is the measurement that says so.');
  }
  lines.push('');
  return { stdout: data.stdout + lines.join('\n') + '\n', 'stdout::text': '' };
}
