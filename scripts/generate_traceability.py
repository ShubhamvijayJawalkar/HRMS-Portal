#!/usr/bin/env python3
"""Render docs/TRACEABILITY.md from traceability.py.

The matrix is generated rather than hand-edited so it cannot drift from the code,
and so the honest `PARTIAL`/`NOT_STARTED` verdicts live next to the evidence for
them. Do not edit the output — edit ``traceability.py`` and re-run this.

    python scripts/generate_traceability.py
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import traceability  # noqa: E402

OUT = REPO_ROOT / 'docs' / 'TRACEABILITY.md'

STATUS_ORDER = ('IMPLEMENTED', 'PARTIAL', 'NOT_STARTED', 'RETIRED')

HEADER = """# SRS v2.0 requirement traceability

_Generated {date} by `scripts/generate_traceability.py` from `traceability.py`.
**Do not hand-edit this file** — edit the data and re-run the script._

Every FR-* requirement in `HRMS_SRS_v2.0.pdf` is listed with the routes that
implement it and an honest verdict. The verdicts are deliberately unforgiving.

| Verdict | Meaning |
|---|---|
| `IMPLEMENTED` | the behaviour is enforced server-side **and** covered by a test |
| `PARTIAL` | a usable subset ships; the note names exactly what is missing |
| `NOT_STARTED` | nothing enforces it, even where a table or column exists |
| `RETIRED` | folded into another requirement by Appendix A |

A requirement is only `IMPLEMENTED` where the code *enforces* it, not where a
handler merely exists. Four tests in `tests/test_app.py` keep this honest: the
matrix must cover exactly the SRS id set, every route it names must exist in the
live `url_map`, an `IMPLEMENTED` row with no route must explain itself, and a
`PARTIAL` row must say what is missing. A renamed route turns the build red
rather than quietly invalidating this document.

## Summary

{summary}

{groups}
## What the gaps have in common

The `NOT_STARTED` rows are not random; they cluster in four places.

**Authentication hardening** — the session, hashing, CSRF and rate-limit work of
Phase 3a is done, but the account-level defences are not. `FR-AUTH-03` (no
consecutive-failure lock, so only the IP rate limit stops a password spray),
`FR-AUTH-10` (no minimum length, no breached-password check) and `FR-AUTH-11`
(no MFA at all) are all absent. MFA is the largest single gap this pass found:
`mfa_credentials` sits in the canonical schema with an encrypted-secret column
and **no code anywhere that reads or writes it** — no enrolment, no challenge,
no gate.

**Schema without routes** — `approval_delegations` (FR-LEA-08a),
`notification_preferences` (FR-NOT-03) and `holiday_optins` (FR-HOL-03) have
their tables and constraints in the canonical schema and no endpoint. The v2.0
target was designed for capabilities the service layer has not caught up with,
which is exactly the kind of drift a traceability matrix is for: without one,
these read as done.

**Synchronous exports** — `FR-LEA-03`, `FR-REG-04` and `FR-RPT-02` all export
inline, so a wide report blocks the request that asked for it. The background-job
machinery the outbox and the CSV importer already prove is available; nothing
reuses it for exports yet.

**Business rules left to the client** — `FR-EXP-03` has no state machine and no
self-approval block, so an employee can approve their own expense claim.
`FR-PERF-01` lets an employee rate their own goal. These are the class of gap
that looks harmless in a demo and is not.

Two `PARTIAL` rows are security properties rather than features, and are worth
reading before any deployment decision:

* **FR-DOC-02** validates the file **extension**, not the content. There is no
  malware scan. A renamed executable passes the allow-list.
* **FR-DOC-03** serves downloads directly rather than by presigned URL, and does
  not audit the read, so document access leaves no trail.
"""

CLOSING = """
## Next, if you want the numbers to move

Roughly in order of (risk x effort):

1. **FR-DOC-02 content sniffing** — small, and it closes the "renamed .exe
   passes" hole. No new dependency needed: read the first bytes and check the
   magic number, which is what "sniffed from content" means in practice.
2. **FR-EXP-03 self-approval block** — one ownership check, mirroring what
   `approve_leave` already does. Closes an approval-integrity gap.
3. **FR-AUTH-10 password policy** — a minimum length and a small offline
   breached-password corpus. The schema needs nothing.
4. **FR-AUTH-03 account lockout** — needs a failure counter and a `locked_until`
   column, so a migration.
5. **FR-AUTH-11 MFA** — the largest item: enrolment flow, encrypted secret
   handling, a TOTP challenge on the login route, and recovery codes. The schema
   is already there, which is the only reason it is worth doing.
"""


def main() -> int:
    counts = traceability.counts()
    total = sum(counts.values())
    summary = ['| Verdict | Count | Share |', '|---|---:|---:|']
    for status in STATUS_ORDER:
        n = counts.get(status, 0)
        share = f'{n / total * 100:.0f}%' if n else '-'
        summary.append(f'| `{status}` | {n} | {share} |')
    summary.append(f'| **total** | **{total}** | |')

    groups = []
    for status in STATUS_ORDER:
        ids = traceability.by_status(status)
        if not ids:
            continue
        groups.append(f'### {status} ({len(ids)})\n')
        groups.append('| ID | Pri | Δ | Routes | Notes |')
        groups.append('|---|---|:---:|---|---|')
        for rid, pri, delta, st, routes, note in traceability.rows():
            if st != status:
                continue
            route_cell = '<br>'.join(f'`{r}`' for r in routes) if routes else '—'
            groups.append(f'| `{rid}` | {pri} | {delta} | {route_cell} | {note} |')
        groups.append('')

    OUT.write_text(HEADER.format(date=date.today(), summary='\n'.join(summary),
                                 groups='\n'.join(groups)) + CLOSING)
    print(f'Wrote {OUT} ({total} requirements: {counts})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
