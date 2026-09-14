"""The detection vocabulary's one pattern construction — shared by the detector and its reports.

`BreakingDetector` matches configured terms with word boundaries over escaped config values, and any
surface that replays a vocabulary ("what would this list have flagged?", ISSUE_121) has to match
**identically** — otherwise it describes a matcher that is not the one running. So the construction
lives here once: the detector compiles the Python form, a report renders the POSIX form for
Postgres' `~*`, and neither owns it.

Two properties travel with it, and both are why a term can silently do nothing:

- **A config value is never a pattern.** Every term is escaped, so `rate.*decision` matches those
  literal characters. The vocabulary is operator-written text, not a regex dialect.
- **Boundaries are exact.** `monetary policy decision` does not match `Monetary policy decisions`,
  which is verbatim the title the ECB publishes for every rate decision — a miss nothing reports
  until a sweep counts zero for a term that should be the most common one in the set.
"""
import re
from typing import Optional, Pattern, Sequence

# Postgres spells the word boundary `\y`, Python spells it `\b`; the escaping differs too, which is
# why the two forms are built separately rather than one string being reused.
_SQL_SPECIAL = re.compile(r'([\\.^$|()\[\]{}*+?])')


def build_keyword_pattern(terms: Sequence[str]) -> Optional[Pattern]:
    """The detector's own pattern: case-insensitive, word-bounded, terms escaped.

    None when the vocabulary is empty — "no keywords configured" is a state, not a match-nothing
    pattern, and the caller decides what it means.
    """
    if not terms:
        return None
    alternation = '|'.join(re.escape(term) for term in terms)
    return re.compile(rf'\b(?:{alternation})\b', re.IGNORECASE)


def sql_keyword_alternation(terms: Sequence[str]) -> str:
    """The same vocabulary for a Postgres `~*` comparison — `\\y(…)\\y`, terms escaped.

    Used to pre-filter rows in SQL so only candidates travel to Python, where the compiled pattern
    above makes the decision. Empty terms yield a pattern matching nothing, so a caller that forgot
    to guard gets an empty result rather than every row.
    """
    if not terms:
        return r'\y(?!)\y'
    return r'\y(' + '|'.join(_SQL_SPECIAL.sub(r'\\\1', term) for term in terms) + r')\y'
