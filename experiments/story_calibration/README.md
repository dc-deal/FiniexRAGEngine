# Story-threshold calibration (ISSUE_96)

`breaking.story_similarity` is the one number in the story rule that cannot be derived — it has to
be measured against ground truth. The ground truth is the **hand count** recorded in ISSUE_82's
follow-up notes: 29 breaking episodes over the seven days to 2026-08-18, read into ~17 stories by
eye, with the groups named per symbol.

This sweeps the threshold over exactly that window and prints what each value produces, so the
default in `configs/pipelines/*.json` is an output of a measurement rather than a guess.

```bash
# on the server, venv active, DATABASE_URL set:
python experiments/story_calibration/calibrate.py
```

Read-only over `outcomes`, no API spend. The window ends 2026-08-18, deliberately: the archive from
**2026-08-20 19:24 UTC to 2026-08-22 09:59 UTC** is contaminated for `crypto_sentiment` (the ingest
worker was dead and the eval workers scored a frozen corpus, ISSUE_97), so a window containing it
measures the outage rather than the news.

**How to read the result.** The row to match is `hand`. A threshold that reproduces it per unit —
not merely in total — is the one to take: the totals can agree while two units are wrong in
opposite directions. If no threshold comes close on the per-unit rows, that is the finding, and the
lexical measure is not sufficient; say so rather than picking the least-bad row.

## Reading the groupings, not just the counts

Counts alone cannot sign off a threshold — two units can be wrong in opposite directions and still
total correctly. `SHOW_AT` prints which episodes were put together, with their reasons, so a merge
can be judged rather than trusted:

```powershell
$env:SHOW_AT="0.45"; python -m experiments.story_calibration.calibrate
```

A merge the hand count disagrees with is not automatically the measure's error. The hand count was
itself an eye-reading of truncated console output; two episodes the measure scores above 0.60 are
textually near-identical, and that is worth re-judging before the threshold moves to accommodate it.
