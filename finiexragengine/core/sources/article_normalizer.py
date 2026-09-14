"""Article text normalisation at ingest — one declared, stamped treatment (ISSUE_112).

Article text reached the embedder, the eval prompt and the breaking keyword match exactly as the
feed served it. Measured over 1,966 dev articles: 50.5 % carry HTML markup, 21.2 % carry entities,
and **36.7 % of every token the engine pays for is markup**. The same markup produced the first
measured false-positive class in the one detection gate that needs no corroboration — 6 of 99
keyword hits were a CDN's stock-image filenames (`…lawsuit-justice-breaking-news.png`) on a
weight-1.0 source, each enough on its own to flag an article HIGH.

Normalising here rather than at each consumer is deliberate: the embedder, the prompt and the
keyword matcher all read `title`/`summary`, so one treatment at the seam where an `Article` is built
fixes three call sites and every future one.

**Why stdlib and not a sanitiser library.** `bleach`, `lxml.html.clean` and BeautifulSoup are the
mainstream HTML→text answers and all three make a *library version* part of what defines the signal
series, without appearing anywhere in `config_fingerprint` — the ISSUE_109 vector-space lesson in a
different column. A pinned profile string plus stdlib keeps the treatment reproducible from the
stamp alone.

The treatment is **pure and idempotent**: normalising twice equals normalising once, which is what
lets a replay re-derive a row's text without knowing whether it was already clean.
"""
import html
import re
import unicodedata
from typing import Optional, Tuple

from finiexragengine.types.article_types import Article
from finiexragengine.types.ingest_types import TEXT_NORMALIZER_PROFILES, TextNormalizerProfile

# Script and style BODIES, not merely their tags. Zero occurrences in the measured corpus, kept
# because stripping only the tags would leave their contents standing in the prompt — and this is
# the one path on our side of the vendor seam where injected text can be removed at all.
_SCRIPT_STYLE = re.compile(r'(?is)<(script|style)\b[^>]*>.*?</\1\s*>')

# One markup tag. The length bound carries its origin: over 11,994 tags measured in the dev corpus
# the p99 is 238 characters and the maximum 2,880, so 400 covers all but a single outlier.
#
# Bounded deliberately, and the direction matters more than the coverage. An unbounded `[^>]*`
# meeting a stray `<` with no closing bracket consumes prose to the end of the field; the bound makes
# the worst case "one tag survives" instead of "the article is gone". The first attempt at measuring
# this problem shipped a character class that deleted almost all text while still producing a
# plausible token count — the failure mode is silent, so the safe direction is chosen on purpose.
_TAG = re.compile(r'<[a-zA-Z/!][^>]{0,400}?>')

_WHITESPACE = re.compile(r'\s+')


class ArticleNormalizer:
    """Applies one declared text profile to an `Article`, keeping the fetched bytes when it changed.

    Args:
        profile: The declared treatment (`ingest.text_normalizer`). Stamped onto every row it
            touches so a vector's text treatment is recorded rather than inferred.
    """

    def __init__(self, profile: TextNormalizerProfile = 'v1') -> None:
        # Strict at the producing seam (CLAUDE.md's closed-vocabulary rule): a profile that does not
        # exist must fail where it is configured, not silently normalise with the wrong treatment
        # and stamp a name nothing implements.
        if profile not in TEXT_NORMALIZER_PROFILES:
            raise ValueError(
                f"unknown text normalizer profile '{profile}' — "
                f'known profiles: {", ".join(TEXT_NORMALIZER_PROFILES)}')
        self._profile: TextNormalizerProfile = profile

    def get_profile(self) -> TextNormalizerProfile:
        return self._profile

    def normalize_text(self, text: str) -> str:
        """The treatment itself — pure, idempotent, and the same for title and summary.

        Order is load-bearing:

        1. drop `<script>`/`<style>` bodies, before anything can separate them from their tags;
        2. strip markup;
        3. unescape entities — after the first strip, so an entity inside an attribute cannot
           become a tag-shaped artefact;
        4. strip again, because step 3 can *reveal* entity-encoded tags (`&lt;p&gt;`);
        5. drop Unicode `Cc`/`Cf` — C0 controls, zero-width joiners, BOM, bidi overrides and
           Unicode Tags, the carriers that hide text from a human reader but not from the model;
        6. NFC, so visually identical text embeds identically;
        7. collapse whitespace — including NBSP and narrow NBSP, which Python's `\\s` matches.
        """
        if not text:
            return ''
        stripped = _SCRIPT_STYLE.sub(' ', text)
        stripped = _TAG.sub(' ', stripped)
        stripped = html.unescape(stripped)
        stripped = _TAG.sub(' ', stripped)
        stripped = ''.join(ch for ch in stripped
                           if unicodedata.category(ch) not in ('Cc', 'Cf'))
        stripped = unicodedata.normalize('NFC', stripped)
        return _WHITESPACE.sub(' ', stripped).strip()

    def apply(self, article: Article) -> Article:
        """Normalise one article in place, keeping the fetched text only where it changed.

        Mutates rather than copies: the article was built moments ago by the source that owns it and
        has no other reader yet, so a copy would buy nothing and cost an allocation on every fetched
        item of every pass (~320 per pass, most of them already known to the corpus).

        `article_id` is untouched by construction — it is hashed from guid/url (ISSUE_3), never from
        text — so dedup, idempotent upsert and replay are unaffected.
        """
        article.title, article.title_raw = self._field(article.title, article.title_raw)
        article.summary, article.summary_raw = self._field(article.summary, article.summary_raw)
        article.text_normalizer = self._profile
        return article

    def _field(self, value: str, existing_raw: Optional[str]) -> Tuple[str, Optional[str]]:
        """Return the normalised field and the original — the original only when it changed.

        NULL raw means "arrived clean", which is the common case and the reason the raw copy costs
        ~633 B on roughly half the corpus instead of doubling it. Keeping the fetched bytes at all is
        what preserves the *"store the full raw corpus, never discard at ingest"* rule: markup is
        removed from what the model reads, not from what the engine holds.

        `existing_raw` is why this takes two arguments. Applying twice to the same article would
        otherwise ERASE the record: the second call sees text that is already clean, concludes
        nothing changed, and writes NULL over the bytes the first call preserved. The text is
        idempotent under this treatment; without this guard the *provenance* would not be, and it is
        the half that cannot be recomputed.
        """
        normalised = self.normalize_text(value)
        if existing_raw is not None:
            return normalised, existing_raw
        return normalised, (value if normalised != value else None)
