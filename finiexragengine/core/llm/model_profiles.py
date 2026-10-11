"""Model profiles — what each model family accepts and needs, resolved from a model id."""
import logging
from typing import Iterable, Tuple

from finiexragengine.types.llm_types import ModelProfile

logger = logging.getLogger(__name__)

# How an unknown model is called: today's behaviour before profiles existed (temperature sent,
# schema as a hint). A model this table does not know is therefore never called differently from
# how it would have been — a new entry is a decision, never a side effect of not knowing.
_DEFAULT = ModelProfile(family='default', accepts_temperature=True, strict_output=False,
                        reasoning=False)

# Families by id prefix, longest match wins. A PREFIX rather than a list of ids: the family is the
# property, and a list would need an entry per dated snapshot (`gpt-5-mini-2025-08-07`).
#
# `gpt-5`: the reasoning family. It rejects `temperature` with a hard 400 (observed 2026-09-20 on
# gpt-5-nano), and with the schema only a hint it returns malformed keys — `urgingency` for
# `urgency` on gpt-5-mini's first pass (2026-10-11), 12 missing `urgency` in the nano trial. Strict
# output constrains decoding to the schema, which fixed both on the same input.
#
# `gpt-4o`: the family the live public series run on (gpt-4o-mini). Its profile is pinned by a
# test: switching it to strict output would change how those series are produced without moving
# any fingerprint, so that is decided like a prompt bump, never as a cleanup.
_PROFILES: Tuple[ModelProfile, ...] = (
    ModelProfile(family='gpt-5', accepts_temperature=False, strict_output=True, reasoning=True),
    ModelProfile(family='gpt-4o', accepts_temperature=True, strict_output=False, reasoning=False),
)

# Fine-tunes are named `ft:<base model>:<org>:…` — the base model decides how the API treats them.
_FINE_TUNE_PREFIX = 'ft:'


class ModelProfiles:
    """Resolves a model id to its family's profile.

    One table for every place that has to know how a model behaves: the provider (what to send),
    the boot log and `models_cli` (what to show). Provider-specific knowledge, but kept out of the
    provider's call path so it can be read and tested on its own.
    """

    def resolve(self, model_id: str) -> ModelProfile:
        """The profile of `model_id` — longest family prefix, fine-tune prefix stripped first."""
        base = model_id.removeprefix(_FINE_TUNE_PREFIX)
        matches = [profile for profile in _PROFILES if base.startswith(profile.family)]
        if not matches:
            return _DEFAULT
        return max(matches, key=lambda profile: len(profile.family))

    def announce(self, model_ids: Iterable[str]) -> None:
        """One `[MODEL]` line per distinct model at boot — how each will be called, before it is.

        Said where the configuration is resolved rather than at the first call, so the boot log
        states it beside `[CONFIG]` even for a stream whose first pass is an hour away.
        """
        for model_id in sorted(set(model_ids)):
            logger.info('[MODEL] %s · %s', model_id, self.resolve(model_id).describe())

