"""Payroll calculation — one function, called from the preview and the run.

FR-PAY-02 / FR-PAY-03 / FR-PAY-04 / FR-PAY-05 of the SRS, and Appendix A-07's
"one function, not two". v1.0 shipped a hard-coded variant inside
``calc_payroll_item`` and a second slab table inside ``calc_tds`` that were not
even the same shape; the payslip could disagree with the TDS report and nobody
would notice. Everything the calculation needs is now **configuration**, read
through :func:`rates_on`, and the same :func:`calc_item` is called by the run
and by the preview endpoint.

**Rates are versioned and effective-dated.** ``payroll_rates`` carries whole
configuration *versions* rather than one row per scalar: a version is applied
all-or-nothing, which is what "a run always uses the rates effective on the
run's period end date" means operationally, and it keeps a mid-flight PF change
from being half-applied to one employee and not to another. Overlapping
versions are refused by an exclusion constraint (the same mechanism as
``no_overlapping_structure``), so the answer to "which rates applied on 31
March" is never ambiguous, even under a concurrent edit.

**TDS is a slab table, not a percentage.** The old ``calc_tds`` encoded one
specific slab table as a literal ladder of ``if`` statements. The slabs now
live inside the versioned row as JSON, are validated (strictly ascending,
first slab at 0, rates in ``[0, 1)``) and are applied cumulatively. The seeded
v2.0 baseline reproduces the old numbers exactly; Finance changes them by
creating a new version, never by editing code.

**Loss of pay is FR-LEA-09's figure, not a new rule.** ``lop_days`` reads the
classification FR-JOB-01 already wrote — ``Absent`` counts 1, ``Half-day``
counts 0.5 — over the **run period**, and the per-day rate is the period's
**per-employee** working days (weekly-off pattern and holidays included).
Two consequences worth stating:

* if the period contains no working day for the employee, ``lop_amount`` is
  zero — a docking rate cannot be computed and inventing one would be a
  fabricated deduction;
* the base is **gross**, because the SRS's own formula subtracts LOP from the
  gross figure: ``net = gross − (Σdeductions + pf + esi + pt + tds) − lop_amount + reimbursements``.

**Reimbursements are what the run will actually pay.** The SRS's §6.8 flow
defines them as ``SUM(expense_claims WHERE status = 'Approved' AND not yet
paid)`` — claims Finance has approved and not yet paid, in full, not filtered
by month (a claim approved after the run was created is still owed). "Not yet
paid" means exactly that here: ``status = 'Approved'`` **and**
``reimbursed_in_run_id IS NULL``. The run links the claims it picked up inside
its own transaction, and finalization marks them ``Paid`` in the same
transaction, so a claim can never be reimbursed twice — not by the next run,
and not by a re-run of the same period.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from fractions import Fraction


class PayrollRatesError(Exception):
    """No effective payroll rates for the requested date, or invalid config."""


@dataclass(frozen=True)
class TDSSlab:
    from_amount: float
    rate: float


@dataclass(frozen=True)
class RateSet:
    version_id: int
    effective_from: date
    effective_to: date | None
    pf_rate: float
    pf_max: float
    esi_rate: float
    esi_max: float
    pt_amount: float
    pt_threshold: float
    tds_slabs: tuple[TDSSlab, ...]

    def as_dict(self) -> dict:
        return {
            'version_id': self.version_id,
            'effective_from': self.effective_from.isoformat(),
            'effective_to': self.effective_to.isoformat() if self.effective_to else None,
            'pf_rate': self.pf_rate,
            'pf_max': self.pf_max,
            'esi_rate': self.esi_rate,
            'esi_max': self.esi_max,
            'pt_amount': self.pt_amount,
            'pt_threshold': self.pt_threshold,
            'tds_slabs': [
                {'from_amount': slab.from_amount, 'rate': slab.rate}
                for slab in self.tds_slabs
            ],
        }


def parse_tds_slabs(raw) -> tuple[TDSSlab, ...]:
    """Validate a TDS slab table (JSON or request body) and return it typed.

    Strictly ascending ``from_amount``, the first slab at 0, rates in
    ``[0, 1)``. Anything else is refused rather than silently normalised —
    a slab table that is "almost right" withholds the wrong tax.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise PayrollRatesError(f'tds_slabs is not valid JSON: {exc}') from exc
    if not isinstance(raw, list) or not raw:
        raise PayrollRatesError('tds_slabs must be a non-empty list')
    parsed: list[TDSSlab] = []
    previous = None
    for entry in raw:
        if not isinstance(entry, dict):
            raise PayrollRatesError('each TDS slab must be an object')
        try:
            from_amount = float(entry['from_amount'])
            rate = float(entry['rate'])
        except (KeyError, TypeError, ValueError) as exc:
            raise PayrollRatesError(
                'each TDS slab needs a numeric from_amount and rate'
            ) from exc
        if from_amount < 0:
            raise PayrollRatesError('TDS slab from_amount must be 0 or more')
        if not 0 <= rate < 1:
            raise PayrollRatesError('TDS slab rate must be in [0, 1)')
        if previous is not None and from_amount <= previous:
            raise PayrollRatesError(
                'TDS slabs must be strictly ascending on from_amount'
            )
        previous = from_amount
        parsed.append(TDSSlab(from_amount, rate))
    if parsed[0].from_amount != 0:
        raise PayrollRatesError('the first TDS slab must start at 0')
    return tuple(parsed)


def slabs_as_json(slabs: tuple[TDSSlab, ...]) -> str:
    return json.dumps(
        [{'from_amount': slab.from_amount, 'rate': slab.rate} for slab in slabs],
        separators=(',', ':'),
    )


def rates_on(conn, as_of: date) -> RateSet:
    """The single rate version in force on ``as_of``.

    Raises :class:`PayrollRatesError` when none is effective — the caller turns
    that into a refusal, because a payroll computed on guessed rates is worse
    than a payroll that does not run.
    """
    row = conn.execute(
        "SELECT version_id, effective_from, effective_to, pf_rate, pf_max, "
        "esi_rate, esi_max, pt_amount, pt_threshold, tds_slabs "
        "FROM payroll_rates "
        "WHERE effective_from <= ? AND (effective_to IS NULL OR effective_to >= ?) "
        "ORDER BY effective_from DESC, version_id DESC LIMIT 1",
        [as_of, as_of],
    ).fetchone()
    if not row:
        raise PayrollRatesError(f'no payroll rates effective for {as_of.isoformat()}')
    version_id, eff_from, eff_to, pf_rate, pf_max, esi_rate, esi_max, pt_amount, pt_threshold, slabs = row
    eff_from = _as_date(eff_from)
    eff_to = _as_date(eff_to) if eff_to else None
    return RateSet(
        version_id=int(version_id),
        effective_from=eff_from,
        effective_to=eff_to,
        pf_rate=float(pf_rate),
        pf_max=float(pf_max),
        esi_rate=float(esi_rate),
        esi_max=float(esi_max),
        pt_amount=float(pt_amount),
        pt_threshold=float(pt_threshold),
        tds_slabs=parse_tds_slabs(slabs),
    )


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def calc_tds(annual_gross: float, slabs: tuple[TDSSlab, ...]) -> float:
    """Cumulative slab tax on an annual figure; the last slab is open-ended."""
    annual = max(float(annual_gross), 0.0)
    total = 0.0
    for index, slab in enumerate(slabs):
        if annual <= slab.from_amount:
            continue
        upper = slabs[index + 1].from_amount if index + 1 < len(slabs) else None
        span_end = annual if upper is None else min(annual, upper)
        total += (span_end - slab.from_amount) * slab.rate
    return round(total, 2)


def claim_reimbursements(conn, emp_id, run_id):
    """Link every approved-and-unpaid claim of this employee to the run.

    One statement, so the set that is linked and the sum that is paid are the
    same set — there is no window between "read the claims" and "mark them".
    Returns ``[(claim_id, amount)]``; the caller stores the same list via
    :func:`calc_item`.
    """
    rows = conn.execute(
        "UPDATE expense_claims SET reimbursed_in_run_id = ? "
        "WHERE emp_id = ? AND status = 'Approved' AND reimbursed_in_run_id IS NULL "
        "RETURNING claim_id, amount",
        [run_id, emp_id],
    ).fetchall()
    return [(int(row[0]), float(row[1])) for row in rows]


def calc_item(conn, *, emp_id, basic, hra, allowances, deductions,
              period_start: date, period_end: date, rates: RateSet,
              claims: list[tuple[int, float]] | None = None) -> dict:
    """Compute one employee's pay item for a period. No writes.

    Returns every figure on the payslip plus the inputs they came from, so the
    preview endpoint can render a full breakdown without re-deriving anything.
    ``claims`` is the exact set the caller will link (the run passes the
    ``claim_reimbursements`` result); when omitted the function *reads* the
    same set for the preview, which is why the preview can never mark a claim
    paid.
    """
    from working_days import lop_days, working_days

    gross = float(basic) + float(hra) + float(allowances)
    pf = min(gross * rates.pf_rate, rates.pf_max)
    esi = gross * rates.esi_rate if gross <= rates.esi_max else 0.0
    pt = rates.pt_amount if gross > rates.pt_threshold else 0.0
    annual_tds = calc_tds(gross * 12, rates.tds_slabs)
    tds = round(annual_tds / 12, 2)

    days = lop_days(conn, emp_id, period_start, period_end)
    per_period_wd = working_days(conn, emp_id, period_start, period_end)
    per_day = (gross / per_period_wd) if per_period_wd else 0.0
    lop_amount = round(per_day * float(days), 2)

    if claims is None:
        # Preview path: report what the run *would* claim, without linking.
        claim_rows = conn.execute(
            "SELECT claim_id, amount FROM expense_claims "
            "WHERE emp_id = ? AND status = 'Approved' AND reimbursed_in_run_id IS NULL",
            [emp_id],
        ).fetchall()
        claims = [(int(row[0]), float(row[1])) for row in claim_rows]
    reimbursements = round(sum(amount for _, amount in claims), 2)

    deductions = float(deductions)
    deductions_total = round(deductions + pf + esi + pt + tds, 2)
    net = round(gross - deductions_total - lop_amount + reimbursements, 2)

    return {
        'gross': round(gross, 2),
        'pf': round(pf, 2),
        'esi': round(esi, 2),
        'pt': round(pt, 2),
        'tds': tds,
        'lop_amount': lop_amount,
        'reimbursements': reimbursements,
        'lop_days': float(days),
        'working_days_in_period': int(per_period_wd),
        'deductions_total': deductions_total,
        'net': net,
        'reimbursement_claim_ids': [claim_id for claim_id, _ in claims],
    }


def monthly_tds(annual_gross: float, slabs: tuple[TDSSlab, ...]) -> float:
    """One month's TDS — used by the report, which needs the same number."""
    return round(calc_tds(annual_gross, slabs) / 12, 2)
