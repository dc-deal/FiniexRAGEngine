"""The mock signal generator (ISSUE_93) — the fixture a consumer builds its backtest against.

Generated data is only useful if it satisfies the same contract as live data, and if it *contains*
the cases the consumer's code has to handle. Two things this suite guards, both learned the hard
way:

* **One parser, both sources.** A generated week must load through the production models. It did
  not, silently: when `trigger_reason` moved to the envelope (ISSUE_9) the generator kept passing it
  to `RunMetadata`, Pydantic dropped the unknown keyword, and every line then claimed `''` — "produced
  before this field existed" — which for a freshly generated fixture is a lie. Nothing failed.
* **The anomaly has to occur.** The consumer's RC-4 mitigation discounts an envelope whose evidence
  is older than one it already acted on. That path can only be exercised if the fixture holds a
  seq/evidence inversion; otherwise it first runs in production.

The generator is invoked as a subprocess, the way an operator runs it — not imported and poked at.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from finiexragengine.types.outcome_types import SentimentEnvelope

_SCRIPT = Path('experiments/mock_signal_data/generate.py')
_CYCLES = 400          # smallest run that reliably contains an inversion at the default seed
_ARCHIVER_KEYS = ('collected_msc', 'collected_msc_timebase')


@pytest.fixture(scope='module')
def week(tmp_path_factory) -> list:
    out = tmp_path_factory.mktemp('mock') / 'week.jsonl'
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), '--cycles', str(_CYCLES), '--out', str(out)],
        capture_output=True, text=True, check=True)
    assert 'inversions (per envelope)' in result.stdout, \
        'the generator stopped reporting inversions, or dropped the unit from the label'
    return [json.loads(line) for line in out.read_text().splitlines() if line.strip()]


def _envelope(line: dict) -> SentimentEnvelope:
    """Parse a line through the production model, minus the archiver's own fields."""
    return SentimentEnvelope(**{k: v for k, v in line.items() if k not in _ARCHIVER_KEYS})


def test_a_generated_week_loads_through_the_production_models(week):
    envelopes = [_envelope(line) for line in week]
    assert len(envelopes) > _CYCLES          # scheduled passes plus the unscheduled ones
    assert {e.schema_version for e in envelopes} == {'2.0'}
    assert {e.data_origin for e in envelopes} == {'synthetic'}


def test_every_line_says_why_its_pass_ran(week):
    """`''` means "produced before this field existed" — a fresh fixture may never claim it."""
    reasons = [_envelope(line).trigger_reason for line in week]
    assert '' not in reasons
    assert set(reasons) == {'scheduled', 'breaking'}
    assert 'trigger_reason' not in week[0]['metadata']    # top level since schema_version 2.0


def test_seq_is_gapless_and_in_commit_order(week):
    envelopes = [_envelope(line) for line in week]
    assert [e.seq for e in envelopes] == list(range(1, len(envelopes) + 1))
    assert {e.stream_epoch for e in envelopes} == {1}
    availability = [e.available_msc for e in envelopes]
    assert availability == sorted(availability), 'seq does not follow availability'


def test_the_evidence_invariants_hold_on_every_row(week):
    """The same three a consumer asserts against live data — see ISSUE_9."""
    violations = []
    evidenced = unevidenced = 0
    for line in week:
        envelope = _envelope(line)
        for row in envelope.result:
            stamps = [s.fetched_at for s in row.sources if s.fetched_at is not None]
            if stamps:
                evidenced += 1
                expected = int(max(stamps).timestamp() * 1000)
                if row.evidence_as_of != expected:
                    violations.append((envelope.seq, row.symbol, 'evidence_as_of != max(fetched_at)'))
            else:
                unevidenced += 1
                if row.evidence_as_of is not None:
                    violations.append((envelope.seq, row.symbol, 'stamp without evidence'))
            if row.evidence_as_of is not None and row.evidence_as_of > envelope.available_msc:
                violations.append((envelope.seq, row.symbol, 'evidence newer than its envelope'))
    assert violations == []
    # Both cases must occur, or the fixture only exercises half the consumer's reader.
    assert evidenced > 0 and unevidenced > 0


def test_the_week_contains_the_seq_evidence_inversion(week):
    """The case the consumer's RC-4 mitigation exists for, found with the rule they apply.

    Note the unit: an inversion is a property of the ENVELOPE (the maximum evidence stamp across its
    rows), never of a single row. Per row the same week yields ~120x more, all of it ordinary — a
    row's stamp falls whenever its retrieved set changes. Measuring per row and calling the result
    an anomaly produces a filter that fires continuously in normal operation.
    """
    inversions = []
    previous = None
    for line in week:
        envelope = _envelope(line)
        evidence = max((r.evidence_as_of for r in envelope.result
                        if r.evidence_as_of is not None), default=None)
        if evidence is not None and previous is not None and evidence < previous:
            inversions.append(envelope.seq)
        previous = evidence if evidence is not None else previous
    assert inversions, 'no envelope carries a higher seq with older evidence'


def test_the_archive_line_declares_its_time_base(week):
    """Mirrors `outcome_exporter`: the archiver's two keys, prepended, nothing inferred."""
    assert list(week[0])[:2] == list(_ARCHIVER_KEYS)
    assert week[0]['collected_msc_timebase'] == 'utc'
