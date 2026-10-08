#!/usr/bin/env python3
"""Extract the SRS's detailed software requirements (the DSR layer) into `srs_spec.py`.

**Why this exists.** `traceability.py` records *what we did* about each requirement —
a verdict, routes, a note. It does not record *what the SRS actually asks for*, so a
reader working from the to-do list had no way to see the requirement itself without
opening the PDF, and the matrix's own `priority`/`delta` columns had nothing to be
checked against. They had drifted on **43 of 104 rows** — seven where the SRS says
High and the matrix said Medium (so the to-do list's "High priority first" ordering
left them out of the High group), seventeen the other way round, and the rest
delta-only. The drift was corrected from §5, and
`test_traceability_records_the_srs_priority_and_delta` now holds the two together.

**It also found nine requirements nobody was tracking.** §5 carries 112 rows, not the
104 the matrix had: `FR-PAY-01..09` were absent entirely, because plain-text extraction
renders their ids as `FR-PAY -01` with a space before the hyphen and the parser —
rightly — did not treat that as an id. Reading the column geometry instead recovers
them, which is why this parses **layout mode** rather than plain text.

The extraction is mechanical, and it follows the table's geometry rather than a set of
text filters:

* `page.extract_text(extraction_mode='layout')` preserves column positions, so §5 is
  visibly a four-column table: `ID | Requirement | Pri | Δ`.
* **A row begins at column 0.** Its first line carries the id, the first line of the
  statement, and — on that same line — `Pri` and `Δ`. Every following line of the
  statement is *indented* to the statement column.
* So a line at column 0 is by construction **not** statement text: it is a row start,
  a module heading (`FR-LEA—Leaves`), the `ID Requirement Pri Δ` column header, the
  page footer, a section heading, or the prose captions between tables. Those are
  skipped by their column, not by matching their words — no keyword filter can miss.
* `Pri` and `Δ` are read off the row's own first line, so a row cannot be merged into
  the one above it: a line that starts like an id but carries no `Pri Δ` terminator is
  an error, not a continuation.
* The page number sits alone on an indented line at the foot of each page, where it
  is appended to the last row on that page. It is stripped as a trailing bare integer;
  no row's statement ends in one (verified: every such trailing part is the page's
  number, 9 through 23 in order).

**Layout mode is authoritative over plain mode, and nothing is repaired after
extraction.** The two disagree on spacing about forty times out of a hundred
requirements, but since the font correction below, only in *one* direction: plain
text invents spaces inside words (`T ea`, `Knowledge T ransfer`, `Http Only`,
`FR-PAY -09`), where layout keeps them together. `FR-PAY -09` in a cross-reference
is a broken reference and `T ea` is at worst ugly, so layout wins on structure
alone — and it used to lose on word gaps too, dropping them for every statement set
in the SRS's bold face (`Approvaldelegation.`, `Theworking-day/`,
`Dailyattendance`, `AppendixA-01`), because that face declared a space the document
never sets and the layout reader divides each real gap by it. The measurement and
the correction are documented where they are implemented, just above `_pdf_lines`.
Because the fix is at the source, `_tidy` only collapses the wrapping now: the
previous `AppendixA` repair was deleted rather than left as a no-op, because a
repair that hides the very defect the extractor no longer has is how a regression
stays invisible.

`FR-ANL-04` is the one requirement with no §5 row: it is a **v1.0** id that Appendix
A-24 records as having its rule restated inside v2.0's `FR-ANL-02`. The matrix tracks
it as its own row, so the extractor carries it with its provenance rather than
silently dropping it or pretending §5 contains it. It is serialised last, after every
§5 row, so the §5 serials are never disturbed by it.

Usage::

    python scripts/extract_srs_spec.py            # rewrite srs_spec.py
    python scripts/extract_srs_spec.py --check    # fail if srs_spec.py is stale

`--check` is what a test runs: it re-extracts from the PDF and compares, so a revised
SRS turns the build red instead of leaving a document that disagrees with the spec.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PDF = ROOT / 'HRMS_SRS_v2.0.pdf'
OUTPUT = ROOT / 'srs_spec.py'

# The TOC repeats every section heading, so the body copy is the *last* match.
_SECTION_5 = re.compile(r'^5\.\s*Functional', re.IGNORECASE)
_SECTION_6 = re.compile(r'^6\.\s*Corrected', re.IGNORECASE)

#: An id at column 0: the shape a row *or a stray cross-reference* starts with.
_ROW_ID = re.compile(r'^FR-[A-Z]+-\d{2,3}[a-z]?')

#: A complete row: ``FR-<MOD>-<NNN>`` , two or more spaces, the statement's first
#: line, ``Pri``, ``Δ``. `.*` is greedy and the terminator is anchored at the end of
#: the line, so the split is taken from the **right** — `FR-AST-01`'s statement runs
#: straight into its priority with no space at all (``…own-assets view,M  C``), and a
#: lazy match would be reading the table backwards to find the last pair.
_ROW = re.compile(
    r'^(FR-[A-Z]+-\d{2,3}[a-z]?)\s\s+(.*\s*)\s*([HMLS—-])\s\s+([CRN—-])\s*$'
)

#: A page number, alone on its own line. Only ever stripped from the **end** of a
#: row's statement, and only after checking that no row's real text ends in one.
_PAGE_NUMBER = re.compile(r'^\d{1,3}$')

#: Requirements the matrix tracks that §5 does not list as a row, with the SRS text
#: that does define them. `FR-ANL-04` is a v1.0 id: Appendix A-24 records the
#: attrition-weight rule being restated as configuration inside v2.0's `FR-ANL-02`.
#: Carrying it keeps the DSR at the same ids as `traceability.py` and the SRS's own
#: id extraction, and `provenance` says plainly where the text came from.
_APPENDIX_ONLY = {
    'FR-ANL-04': (
        'Appendix A-24 (v1.0 id; v2.0 restates the rule inside FR-ANL-02)',
        'Attrition-risk formula weights (0.4, 1.5, 0.8, 3) must be configuration, '
        'reviewed with HR before go-live, not fixed constants.',
        'S',
        'C',
    ),
}


# --------------------------------------------------------------------------
# pypdf layout mode: the rows whose words come out joined
# --------------------------------------------------------------------------
#
# Layout mode turns a horizontal gap between two text-show operations into
# spaces with ``round(excess_tx / space_tx)``, where ``space_tx`` is one space's
# width *as the font declares it*. Three of the SRS's five faces agree with
# their own typesetting and extract correctly: DejaVuSans declares 419 units and
# its word gaps are 317-318, DejaVuSansMono declares 602 and its gaps are 602.
#
# The **bold** face does not: `YIYFYM+DejaVuSans-Bold` declares 838, but every
# word gap the document emits in that face is 248-394 units, so the ratio is
# 0.30-0.47 and rounds to **zero**. Every statement set in that face is printed
# with its words run together — `Approvaldelegation.`, `Theworking-day/`,
# `Dailyattendance` — while the surrounding columns extract perfectly, which is
# what made it look like a parser problem rather than a font one.
#
# The word space is *measurable from the document*, so measure it instead of
# trusting a width the document contradicts: the most frequent TJ adjustment of
# a font is its word space, because word gaps repeat constantly and letter kerns
# do not. The correction is then applied **only** where the declared width would
# round the document's own gap to zero spaces — that condition *is* the defect,
# so a font the document agrees with is never touched, and a font whose kerns
# are all letter-sized (below a quarter of the declared space) is rejected
# rather than mistaken for a word space.
_MEASURED_WORD_GAPS: dict[str, float] = {}
_GAP_FIX_INSTALLED = False


def _measure_word_gaps(reader) -> dict[str, float]:
    """The most frequent TJ adjustment of each embedded font, keyed by font name."""
    from collections import Counter, defaultdict

    from pypdf.generic import ContentStream

    counted: dict[str, Counter] = defaultdict(Counter)
    for page in reader.pages:
        fonts = page.get('/Resources', {}).get('/Font', {})
        names: dict[str, str] = {}
        for resource, ref in fonts.items():
            font = ref.get_object()
            base = font.get('/BaseFont')
            if base is None:
                descriptor = font.get('/FontDescriptor')
                if descriptor is not None:
                    base = descriptor.get_object().get('/FontName')
            if base is not None:
                names[str(resource)] = str(base).lstrip('/')
        current = None
        for operands, operator in ContentStream(page.get_contents(), reader).operations:
            if operator == b'Tf':
                current = names.get(str(operands[0]))
            elif operator == b'TJ' and current is not None:
                for item in operands[0]:
                    if isinstance(item, (int, float)) and not isinstance(item, bool):
                        counted[current][abs(float(item))] += 1
    return {
        name: counts.most_common(1)[0][0]
        for name, counts in counted.items()
        if counts
    }


def _install_gap_fix(gaps: dict[str, float]) -> None:
    """Teach pypdf's layout mode the word space *this document* uses.

    Idempotent: the measured gaps are refreshed on every call, the wrapper is
    installed once. Installed on `PageObject` because that is where layout mode
    builds its `Font` objects, and it is the only place the wrong `space_width`
    can be corrected before `TextStateParams` divides a real gap by it.
    """
    global _GAP_FIX_INSTALLED
    _MEASURED_WORD_GAPS.clear()
    _MEASURED_WORD_GAPS.update(gaps)
    if _GAP_FIX_INSTALLED:
        return
    from pypdf import PageObject

    original = PageObject._layout_mode_fonts

    def _layout_mode_fonts(self):
        fonts = original(self)
        for font in fonts.values():
            declared = float(font.space_width or 0)
            gap = _MEASURED_WORD_GAPS.get(font.name)
            if declared and gap is not None and 0.2 * declared <= gap < 0.5 * declared:
                font.space_width = gap
        return fonts

    _layout_mode_fonts.__doc__ = (
        (original.__doc__ or '').rstrip()
        + '\n\n    Rebound by `extract_srs_spec._install_gap_fix` for the SRS: the bold '
        'face declares a space twice the width the document actually sets, which makes '
        'layout mode round every word gap to zero spaces.'
    )
    PageObject._layout_mode_fonts = _layout_mode_fonts
    _GAP_FIX_INSTALLED = True


def _pdf_lines(path: pathlib.Path = PDF) -> list[str]:
    """Every line of the SRS, in page order, laid out as it is printed.

    Layout mode rather than plain: see the module docstring for why, but the short
    version is that plain extraction loses the ids of all nine payroll requirements.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise SystemExit(
            'pypdf is required to read the SRS (pip install -r requirements-dev.txt); '
            f'without it srs_spec.py cannot be verified against the spec ({exc})'
        ) from exc
    reader = PdfReader(str(path))
    _install_gap_fix(_measure_word_gaps(reader))
    pages = [page.extract_text(extraction_mode='layout') or '' for page in reader.pages]
    return '\n'.join(pages).split('\n')


def extract(lines: list[str] | None = None) -> list[dict]:
    """The §5 requirement rows, in document order, plus appendix-only ids."""
    if lines is None:
        lines = _pdf_lines()
    # The TOC copy of each heading comes first; the body copy is the last one.
    start = max(n for n, line in enumerate(lines) if _SECTION_5.match(line))
    end = max(n for n, line in enumerate(lines) if _SECTION_6.match(line))
    body = lines[start:end]

    rows: list[dict] = []
    current: dict | None = None
    for line in body:
        if not line.strip():
            continue
        match = _ROW.match(line)
        if match:
            current = {
                'id': match.group(1),
                'statement': [match.group(2)],
                'priority': match.group(3),
                'delta': match.group(4),
                'provenance': '§5',
            }
            rows.append(current)
            continue
        # Column 0 is never statement text in this table; anything else is.
        if line[0].isspace() and current is not None:
            current['statement'].append(line)

    # An id-shaped line at column 0 that carried no `Pri Δ` would have been skipped
    # as furniture, i.e. the requirement would silently vanish. That is the one
    # failure this parser must not have, so it is an error naming the line.
    dropped = [line for line in body if _ROW_ID.match(line) and not _ROW.match(line)]
    if dropped:
        raise ValueError(
            'id lines in §5 that do not carry a Pri/Δ pair (the row would be '
            f'skipped): {dropped}'
        )

    seen: set[str] = set()
    for row in rows:
        if row['id'] in seen:
            raise ValueError(f'duplicate requirement id in §5: {row["id"]}')
        seen.add(row['id'])
        parts = [part for part in row['statement'] if part.strip()]
        while parts and _PAGE_NUMBER.match(parts[-1].strip()):
            # The page number is appended to whichever row ends that page.
            parts.pop()
        row['statement'] = _tidy(' '.join(parts))
        if not row['statement']:
            raise ValueError(f'requirement with an empty statement: {row["id"]}')

    for rid, (provenance, statement, priority, delta) in _APPENDIX_ONLY.items():
        rows.append({
            'id': rid,
            'statement': statement,
            'priority': priority,
            'delta': delta,
            'provenance': provenance,
        })
    return rows


def _tidy(text: str) -> str:
    """Collapse the wrapping. Nothing else: there is no longer anything to repair.

    This used to end with ``text.replace('AppendixA', 'Appendix A')``, because
    layout mode dropped the space in all thirteen appendix pointers. The drop was
    never a parser problem — the bold face declared a space wider than any gap the
    document sets, so layout rounded every one of them to zero spaces — and
    `_install_gap_fix` now corrects that where the space is computed, for *all*
    words, not just the one string a repair happened to know about.

    The replacement was deleted rather than kept as a no-op for the same reason
    the general repair it sat beside was never written: splitting before every
    capital would fix nothing now and would break ``HttpOnly``, ``SameSite`` and
    ``iCal``, which this extraction already gets right. A repair that can only
    hide a regression in the correction above it does not earn its place.
    """
    return ' '.join(text.split()).strip()


def render(rows: list[dict]) -> str:
    """The contents of `srs_spec.py` for these rows."""
    out = [
        '"""Detailed software requirements (DSR): the SRS\'s own requirement text.',
        '',
        '**Generated by `scripts/extract_srs_spec.py` from `HRMS_SRS_v2.0.pdf` §5.**',
        'Do not edit by hand — `test_srs_spec_matches_the_srs_pdf` re-extracts the PDF',
        'and fails if this file disagrees with it.',
        '',
        'Where `traceability.py` answers *what is the state of this requirement*, this',
        'module answers *what does the SRS actually ask for*, in the SRS\'s own order.',
        'That order is what the serial numbers are allocated from, so `SR-042` is a',
        'permanent name for one requirement: a commit, a test or a conversation can cite',
        'it and it will still mean the same thing after other requirements close.',
        '',
        'Serial numbers are stable for the life of an SRS revision. A revised SRS may',
        'insert a requirement and renumber everything after it; that is recorded in the',
        'UPDATE_LOG of `scripts/update_todo_pdf.py` rather than papered over.',
        '"""',
        '',
        'from __future__ import annotations',
        '',
        'from typing import NamedTuple',
        '',
        '',
        'class Requirement(NamedTuple):',
        f'    serial: str      # SR-001 .. SR-{len(rows):03d}, assigned in SRS document order',
        '    id: str          # FR-<MOD>-<NNN> as printed in the SRS',
        '    priority: str    # H | M | L | S | — (the SRS\'s own Pri column)',
        '    delta: str       # C | R | N (the SRS\'s own Δ column)',
        '    provenance: str  # where the statement is printed',
        '    statement: str   # the detailed software requirement',
        '',
        '',
        'REQUIREMENTS: tuple[Requirement, ...] = (',
    ]
    for index, row in enumerate(rows, start=1):
        out.append(
            '    Requirement('
            f"'SR-{index:03d}', "
            f"{row['id']!r}, "
            f"{row['priority']!r}, "
            f"{row['delta']!r}, "
            f"{row['provenance']!r}, "
            f"{row['statement']!r}),"
        )
    out += [
        ')',
        '',
        '',
        'BY_ID: dict[str, Requirement] = {r.id: r for r in REQUIREMENTS}',
        'ORDER: tuple[str, ...] = tuple(r.id for r in REQUIREMENTS)',
        '',
        '',
        'def serial(rid: str) -> str:',
        '    """`SR-001`-style serial for a requirement id.',
        '',
        '    Raises `KeyError` for an id the SRS does not carry, which is what should',
        '    happen: a requirement the spec does not name has no serial to give it.',
        '    """',
        '    return BY_ID[rid].serial',
        '',
        '',
        'def statement(rid: str) -> str:',
        '    """The SRS\'s requirement text for `rid`."""',
        '    return BY_ID[rid].statement',
        '',
    ]
    return '\n'.join(out) + '\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true',
                        help='exit non-zero if srs_spec.py does not match the PDF')
    args = parser.parse_args(argv)

    rendered = render(extract())
    if args.check:
        current = OUTPUT.read_text() if OUTPUT.exists() else ''
        if current != rendered:
            print('srs_spec.py is stale: re-run scripts/extract_srs_spec.py',
                  file=sys.stderr)
            return 1
        print(f'{OUTPUT.name} matches {PDF.name}')
        return 0

    OUTPUT.write_text(rendered)
    print(f'wrote {OUTPUT.name} ({len(rendered.splitlines())} lines)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
