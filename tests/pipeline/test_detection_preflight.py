"""Detection-threshold preflight (ISSUE_106) — can these thresholds still fire?

Pure logic: config objects in, verdict lines out. No DB, no network, no clock.

The cases are written as the *argument for the check existing*, because that is what has to survive:
each one is a state the engine reached or can plausibly reach, and the assertion is what an operator
would have needed to see at that moment.
"""
from finiexragengine.core.pipeline.detection_preflight import (
    check_detection_reachability,
    format_reachability_lines,
)
from finiexragengine.types.config_types.source_set_types import (
    DetectionConfig,
    SourceConfig,
    SourceSetConfig,
)


def _set(*feeds, mid: int = 3, high: int = 5, gate: float = 0.9) -> SourceSetConfig:
    """A set from `(source_id, weight, enabled)` triples."""
    return SourceSetConfig(
        source_set_id='crypto_news',
        detection=DetectionConfig(mid_cluster_size=mid, high_cluster_size=high,
                                  keyword_source_weight=gate, keywords=['hack', 'SEC']),
        sources=[SourceConfig(source_id=sid, url=f'https://{sid}.test/rss',
                              weight=weight, enabled=enabled)
                 for sid, weight, enabled in feeds])


def test_the_case_that_filed_the_issue():
    # crypto_news as it actually ran on 2026-08-25: six declared feeds, two switched off per
    # machine, and a HIGH threshold of 5 that four feeds cannot reach between them. It had been
    # that way for weeks and no surface said a word.
    reach = check_detection_reachability(_set(
        ('cryptonews', 0.8, True), ('cointelegraph', 1.0, True),
        ('decrypt', 1.0, True), ('coindesk', 1.0, True),
        ('theblock', 1.0, False), ('cryptoslate', 0.8, False)))

    assert (reach.declared, reach.active) == (6, 4)
    assert reach.cluster_needs_self_duplication is True
    assert reach.satisfiable is False
    lines = '\n'.join(format_reachability_lines(reach))
    assert '4 active feeds (6 declared, 2 out: theblock, cryptoslate)' in lines
    # Worded as an indicator, NOT as a proof: `count_neighbors` counts corpus articles, so one feed
    # duplicating itself still gets there. Claiming "unreachable" would be false and reassuring.
    assert 'cannot be reached by these feeds alone' in lines
    assert 'unreachable' not in lines


def test_the_keyword_gate_above_every_active_feed_is_a_proof_and_says_so():
    # The failure that leaves NO trace: a keyword hit that never fires writes nothing, logs nothing
    # and flags nothing. Half the detection system switches off in silence. Unlike the cluster
    # check this one is exact — `source_weight` comes from the config and nothing else can raise it.
    reach = check_detection_reachability(_set(
        ('cryptonews', 0.8, True), ('cryptopolitan', 0.8, True), ('sec_press', 0.8, True),
        ('cointelegraph', 1.0, False), ('coindesk', 1.0, False)))

    assert reach.keyword_path_dead is True
    assert reach.max_active_weight == 0.8
    assert reach.satisfiable is False
    lines = '\n'.join(format_reachability_lines(reach))
    assert 'CANNOT fire' in lines
    assert 'detection is cluster-only' in lines


def test_a_healthy_set_reports_the_gate_distribution_rather_than_a_boolean():
    # ISSUE_82 shelved the weight scale as "an unmeasured hand-set constant — two levels, never
    # checked against outcomes" for want of an instrument. The distribution IS the instrument: a
    # gate that every feed clears is a constant with extra steps, and one that two of twenty clear
    # is a different lever than the config suggests.
    reach = check_detection_reachability(_set(
        ('cryptonews', 0.8, True), ('cryptopolitan', 0.8, True),
        ('cointelegraph', 1.0, True), ('decrypt', 1.0, True),
        ('coindesk', 1.0, True), ('beincrypto', 1.0, True), ('theblock', 1.0, True)))

    assert reach.satisfiable is True
    assert (reach.at_or_above_gate, reach.active) == (5, 7)
    lines = '\n'.join(format_reachability_lines(reach))
    assert 'keyword gate 0.9 · 5 of 7 active feeds at or above (highest 1.0)' in lines
    assert 'cluster thresholds 3/5 satisfiable by 7 active feeds' in lines
    assert 'CANNOT' not in lines


def test_growth_flips_the_question_instead_of_answering_it():
    # ISSUE_107's Tier B: at twenty active feeds nothing is out of reach, and a cluster of five
    # stops being a burst — it is a normal story on five outlets. The preflight cannot decide that
    # (it is a calibration question), but it must not report "satisfiable" as if all were well
    # without showing the count the decision needs.
    feeds = [(f'feed{index:02d}', 1.0 if index < 5 else 0.6, True) for index in range(20)]
    reach = check_detection_reachability(_set(*feeds))

    assert (reach.active, reach.satisfiable) == (20, True)
    assert reach.at_or_above_gate == 5           # the gate admits a quarter of the fleet
    assert 'satisfiable by 20 active feeds' in '\n'.join(format_reachability_lines(reach))


def test_a_long_parked_catalogue_names_a_few_and_counts_the_rest():
    # ISSUE_107 parks eighteen candidates with `enabled: false`. Naming all of them would bury the
    # number that matters in a list nobody reads — same `+N more` idiom as the [OVERRIDE] line.
    feeds = [('live', 1.0, True)] + [(f'parked{index:02d}', 0.8, False) for index in range(10)]
    reach = check_detection_reachability(_set(*feeds, high=1))
    line = format_reachability_lines(reach)[0]

    assert '10 out: parked00, parked01, parked02, parked03 +6 more' in line


def test_an_empty_set_does_not_claim_a_satisfiable_threshold():
    # Every feed switched off — a real state on a machine whose egress is walled. The weight check
    # must not divide by nothing and call it healthy.
    reach = check_detection_reachability(_set(('cryptonews', 0.8, False)))

    assert (reach.active, reach.max_active_weight) == (0, 0.0)
    assert reach.keyword_path_dead is True
    assert reach.satisfiable is False


def test_the_boot_line_and_the_report_share_one_wording():
    # The divergence ISSUE_82 spent weeks on was two derivations of one thing. The boot log and the
    # `breaking` report render from the same function; only the tag differs.
    reach = check_detection_reachability(_set(('cryptonews', 1.0, True)))
    tagged = format_reachability_lines(reach, prefix='[DETECTION] ')
    bare = format_reachability_lines(reach)

    assert all(line.startswith('[DETECTION] ') for line in tagged)
    assert [line.removeprefix('[DETECTION] ') for line in tagged] == bare


# --- the read-time half: quarantine (ISSUE_106) -------------------------------------------

def test_quarantine_is_only_known_at_read_time_and_the_wording_says_which_count_it_used():
    # The boot line and the report deliberately report DIFFERENT numbers: `enabled` at boot (a
    # config fact) and `pollable` at read time (config minus whatever the health policy has out).
    # Saying "active feeds" on both would present a boot-time claim as a live one — and a verdict
    # that does not name its population cannot be checked.
    from finiexragengine.core.pipeline.detection_preflight import with_quarantine

    boot = check_detection_reachability(_set(
        ('cryptonews', 0.8, True), ('cointelegraph', 1.0, True),
        ('decrypt', 1.0, True), ('coindesk', 1.0, True),
        ('beincrypto', 1.0, True), ('theblock', 1.0, True)))
    assert boot.quarantine_known is False
    assert boot.effective == boot.active == 6
    assert boot.satisfiable is True
    boot_text = '\n'.join(format_reachability_lines(boot))
    assert 'quarantine not included (it is dynamic' in boot_text

    # Two feeds in cool-off right now: six enabled, four pollable — and HIGH=5 is out of reach.
    live = with_quarantine(boot, {'theblock', 'coindesk'})
    assert live.quarantine_known is True
    assert (live.active, live.effective) == (6, 4)
    assert live.cluster_needs_self_duplication is True
    assert live.satisfiable is False
    live_text = '\n'.join(format_reachability_lines(live))
    assert '4 of 6 enabled feeds pollable' in live_text
    assert '2 quarantined right now: coindesk, theblock' in live_text
    assert 'exceeds the pollable feed count (4)' in live_text


def test_another_sets_quarantine_never_counts_against_this_one():
    # The health store is engine-wide. Counting forex's quarantined feed against crypto would
    # understate crypto's reach and invent a warning nobody can act on.
    from finiexragengine.core.pipeline.detection_preflight import with_quarantine

    reach = check_detection_reachability(_set(('cryptonews', 1.0, True), ('decrypt', 1.0, True)))
    live = with_quarantine(reach, {'fxstreet', 'actionforex'})

    assert live.quarantined_ids == []
    assert live.effective == 2
    assert 'none quarantined right now' in '\n'.join(format_reachability_lines(live))


def test_a_clean_read_says_so_rather_than_staying_silent():
    # "none quarantined right now" is rendered explicitly: a missing clause would be
    # indistinguishable from a surface that never looked.
    from finiexragengine.core.pipeline.detection_preflight import with_quarantine

    live = with_quarantine(check_detection_reachability(_set(('cryptonews', 1.0, True))), set())
    text = '\n'.join(format_reachability_lines(live))
    assert 'none quarantined right now' in text
    assert 'quarantine not included' not in text
