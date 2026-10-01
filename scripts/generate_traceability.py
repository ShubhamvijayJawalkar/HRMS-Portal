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

Eight `NOT_STARTED` rows remain, and they cluster in four places.

**No account-level authentication defence is missing any more.** Phase 3a did
the session, hashing, CSRF and rate-limit work; FR-AUTH-10 added the password
policy; FR-AUTH-11 added TOTP; and FR-AUTH-03 added the lockout. What is left
is a `PARTIAL` row: **FR-AUTH-01**'s login rate limit is keyed on remote address
rather than on the account *and* the IP, which is the right order of magnitude but
not the right key.

**Schema without routes** — `approval_delegations` (FR-LEA-08a) still has its
table and its no-overlap exclusion constraint in the canonical schema and no
endpoint, so a manager going on leave has no way to delegate. This was the shape
of three rows before the matrix caught them, and `notification_preferences` and
`holiday_optins` were both fixed this way — the v2.0 target was designed for
capabilities the service layer had not caught up with, which is exactly the kind
of drift a traceability matrix is for.

**Missing endpoints, not missing logic** — `FR-USR-07` (no bulk user
create/archive endpoint), `FR-LEA-07` (no manual leave grant) and `FR-REG-04`
(no regularization export). Each is a route over rules that already exist
elsewhere: the single-employee archive, the policy-derived balance, and the
report export family.

**Inconsistency by duplication** — `FR-LEA-09` asks for one working-day and
holiday-deduction function. There isn't one: leave day counting, payroll LOP and
the reports each approximate it differently. So the same absence is deducted in
three places and the three disagree, which is the specific failure the
requirement exists to prevent.

**Deployment, not code** — `FR-JOB-05` (no scheduler leader election, so a
multi-pod deployment runs every cron job once per pod), `FR-JOB-03` (no
quarterly job opening the next review cycle) and `FR-ANL-04` (analytics weights
are literals in the handler).

The `PARTIAL` rows are worth reading before any deployment decision, because
several are security properties rather than features:

* **FR-AUTH-09** — the reset token is stored unhashed, and expires in 1 h where
  the SRS asks for 24 h.
* **FR-AUTH-13** — `/api/credentials` has no 5-minute re-authentication, and a
  credential read is not audited.
* **FR-DOC-02** — uploads are validated by magic number for the claimed
  extension and reject the EICAR marker, but there is no real scanner and no
  per-category size cap.
* **FR-DOC-03** — downloads are served directly rather than by presigned URL.
  The read *is* audited now.
"""

CLOSING = """
## Next, if you want the numbers to move

Roughly in order of (risk x effort):

1. **FR-AUTH-09 hashed reset token** — store the SHA-256 of the token instead of
   the token. One line in the write and one in the read; as it stands, a database
   read is a usable credential. It is now the only remaining account-level
   weakness that is a code change rather than a decision.
2. **FR-AUTH-01 per-account rate limiting** — the limit is keyed on remote
   address, so it is bypassed by distributing attempts across sources. Keying a
   second limit on the employee ID (inside the lockout, which already exists)
   closes the spray the IP limit was never going to stop.
3. **FR-LEA-09 one day-counting function** — three implementations currently
   disagree about how many days a leave spans, and the payroll figure is the one
   that costs an employee money.
4. **FR-LEA-08a approval delegation** — the schema is already there and correct;
   this is a route and an audit action.
5. **FR-AUTH-13 re-authentication for credential reads** — a five-minute
   freshness check plus the audit row.
6. **FR-USR-07 / FR-LEA-07 batch and manual-grant routes** — both are thin
   wrappers over rules that already exist elsewhere.

The `PARTIAL` rows that need a decision rather than code are worth more than
several of these: `FR-NOT-03`'s `email` preference is stored and reported but
**nothing sends email automatically**, and widening `ALL_SCOPE_ROLES` beyond
Admin/Super Admin is a product decision that changes who sees which
company-wide lists.
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
