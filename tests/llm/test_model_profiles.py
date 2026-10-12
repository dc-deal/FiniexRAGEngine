"""ModelProfiles — how a model id resolves to its family's call profile."""
import pytest

from finiexragengine.core.llm.model_profiles import ModelProfiles
from finiexragengine.types.llm_types import ModelProfile


@pytest.mark.parametrize('model_id, family', [
    ('gpt-5', 'gpt-5'),
    ('gpt-5-mini', 'gpt-5'),
    ('gpt-5-mini-2025-08-07', 'gpt-5'),          # a dated snapshot is its family
    ('gpt-5-nano', 'gpt-5'),
    ('gpt-4o-mini', 'gpt-4o'),
    ('ft:gpt-4o-mini:org:custom:abc', 'gpt-4o'),  # a fine-tune is its base model
    ('ft:gpt-5-mini:org:custom:abc', 'gpt-5'),
    ('llama-3-70b', 'default'),                   # unknown: called exactly as before profiles
])
def test_a_model_id_resolves_to_its_family(model_id: str, family: str) -> None:
    assert ModelProfiles().resolve(model_id).family == family


def test_an_unknown_model_keeps_the_behaviour_it_had_before_profiles() -> None:
    profile = ModelProfiles().resolve('some-self-hosted-model')
    assert (profile.accepts_temperature, profile.strict_output, profile.reasoning) == (
        True, False, False)


def test_the_profiles_of_the_live_series_are_pinned() -> None:
    # gpt-4o-mini produces the public reference series. Changing how it is called — strict output,
    # no temperature — would change those series without moving any fingerprint. A red here is a
    # decision to take like a prompt bump, with a before/after distribution, never a cleanup.
    assert ModelProfiles().resolve('gpt-4o-mini') == ModelProfile(
        family='gpt-4o', accepts_temperature=True, strict_output=False, reasoning=False)
    assert ModelProfiles().resolve('gpt-5-mini') == ModelProfile(
        family='gpt-5', accepts_temperature=False, strict_output=True, reasoning=True,
        call_timeout_seconds=120.0)


def test_only_the_reasoning_family_declares_its_own_timeout() -> None:
    # Everything else keeps the global `llm.timeout_seconds` — a hung 2 s call must not wait 120 s.
    assert ModelProfiles().resolve('gpt-5-nano').call_timeout_seconds == 120.0
    assert ModelProfiles().resolve('gpt-4o-mini').call_timeout_seconds is None
    assert ModelProfiles().resolve('some-self-hosted-model').call_timeout_seconds is None


def test_the_boot_wording() -> None:
    assert ModelProfiles().resolve('gpt-5-mini').describe() == (
        'family gpt-5 · reasoning · strict output · no temperature · timeout 120 s')
    assert ModelProfiles().resolve('gpt-4o-mini').describe() == 'family gpt-4o · temperature'
