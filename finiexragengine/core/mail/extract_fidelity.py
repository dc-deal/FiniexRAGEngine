"""How faithfully an extract kept its source's numbers (#138) — the cheapest check of an LLM extract.

An invented figure is the error that matters most in a factual extract, and it is the one a machine can
look for: every multi-digit number in a story should stand in the mail's text, in either decimal notation
(`114,48` in a German source, `114.48` in the English story). A number not found is not proof of an
invention — a conversion (`35,6 Cent` → `€0.356`) or a spelled-out number (`anderthalb` → `1.5`) misses
too — so the ratio is a pointer for the judge, not a verdict. Measured on 81 issues of one briefing:
1,322 of 1,354 literal, the rest conversions, spelled-out numbers and German thousands notation.
"""
import re
from typing import Iterable, Tuple

_NUMBER = re.compile(r'\d[\d.,]*\d')
_SWAP = str.maketrans(',.', '.,')        # 5,812.50 ↔ 5.812,50


def numbers_found(text: str, source: str) -> Tuple[int, int]:
    """(found, total) for the multi-digit numbers of `text` that also stand in `source`."""
    found = total = 0
    for number in _NUMBER.findall(text):
        number = number.rstrip('.,')
        total += 1
        variants = {number, number.replace('.', ','), number.replace(',', '.'), number.replace(',', ''),
                    number.replace(',', '').replace('.', ','), number.translate(_SWAP)}
        found += any(_standalone(variant).search(source) for variant in variants)
    return found, total


def _standalone(number: str) -> re.Pattern[str]:
    """The number as a whole: `12` must not be found inside `5.812,50`."""
    return re.compile(rf'(?<![\d.,]){re.escape(number)}(?![\d]|[.,]\d)')


def stories_fidelity(stories: Iterable[dict], source: str) -> Tuple[int, int]:
    """(found, total) over every story's facts and headline."""
    found = total = 0
    for story in stories:
        item_found, item_total = numbers_found(f"{story.get('headline', '')} {story.get('facts', '')}", source)
        found, total = found + item_found, total + item_total
    return found, total
