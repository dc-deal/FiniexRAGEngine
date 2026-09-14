"""Every string in a published config model is classified, by hand, before it can be served.

`GET /v1/configs/{name}` publishes the effective configuration so a remote diagnosis can read what a
machine actually runs. The config models hold three credentials — and the risk is not those three,
which are written down; it is the fourth one, added a year from now to a model nobody re-reads while
a route quietly serves it.

So this sweep walks the model trees themselves and asserts that every string leaf is *classified*:
either named as sensitive or named as public. A new string field fails here until someone decides
which it is. That is the whole point — the alternative, a denylist of secret-looking names, is a
heuristic about names rather than a decision about fields, and Spring Boot Actuator's default
(`password|secret|key|token|…`) would mask `bot_token` while publishing `chat_id`.
"""
import typing
from typing import Any, Dict, List, Set, Tuple, Type

import pytest
from pydantic import BaseModel

from finiexragengine.configuration.config_redaction import (
    CLASSIFIED,
    PUBLIC_PATHS,
    SENSITIVE_PATHS,
)
from finiexragengine.types.config_types.app_config_types import AppConfig
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig
from finiexragengine.types.config_types.source_set_types import SourceSetConfig

_ROOTS: Tuple[Type[BaseModel], ...] = (AppConfig, PipelineConfig, SourceSetConfig)


def _unwrap_optional(annotation: Any) -> Any:
    """`Optional[X]` is `Union[X, None]` — the shape almost every config field uses."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union or str(origin) == "<class 'types.UnionType'>":
        args = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _string_leaves(model: Type[BaseModel], prefix: str = '',
                   seen: Tuple[Type[BaseModel], ...] = ()) -> Set[str]:
    """Every path under `model` whose value is a string, with `*` for a key or an index."""
    if model in seen:
        return set()                       # a recursive model would otherwise not terminate
    paths: Set[str] = set()
    for name, field in model.model_fields.items():
        annotation = _unwrap_optional(field.annotation)
        path = f'{prefix}{name}'
        origin = typing.get_origin(annotation)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            paths |= _string_leaves(annotation, f'{path}.', seen + (model,))
            continue
        if origin in (list, dict):
            args = typing.get_args(annotation)
            assert args, f'{path}: a bare list/dict publishes values nothing can type-check'
            inner = _unwrap_optional(args[-1])
            assert inner is not Any, (
                f'{path}: an `Any` leaf cannot be classified by type — give it a real annotation '
                f'or the exposure policy is guessing')
            if isinstance(inner, type) and issubclass(inner, BaseModel):
                paths |= _string_leaves(inner, f'{path}.*.', seen + (model,))
            elif inner is str or typing.get_origin(inner) is typing.Literal:
                paths.add(f'{path}.*')
            continue
        assert annotation is not Any, f'{path}: an `Any` leaf cannot be classified by type'
        if annotation is str or origin is typing.Literal:
            paths.add(path)
    return paths


def test_every_string_a_config_model_can_publish_is_classified():
    """The guard. A field added later is red here before it is served."""
    unclassified = sorted(path
                          for root in _ROOTS
                          for path in _string_leaves(root)
                          if path not in CLASSIFIED)

    assert not unclassified, (
        'these config strings are not classified in configuration/config_redaction.py — decide '
        'whether each is a credential (SENSITIVE_PATHS) or safe to publish (PUBLIC_PATHS):\n  '
        + '\n  '.join(unclassified))


def test_the_policy_names_nothing_that_no_longer_exists():
    """A stale entry is not harmless: it is a field someone believes is being protected."""
    live = {path for root in _ROOTS for path in _string_leaves(root)}
    stale = sorted(path for path in CLASSIFIED if path not in live)

    assert not stale, ('configuration/config_redaction.py names paths the models no longer have:\n  '
                       + '\n  '.join(stale))


def test_no_path_is_both_public_and_secret():
    """A contradiction must not be resolved by whichever check runs first."""
    assert not (set(SENSITIVE_PATHS) & set(PUBLIC_PATHS))


def test_the_three_credentials_are_the_ones_we_think_they_are():
    """Written out rather than counted: the list changing should require reading this line.

    `DATABASE_URL` and `OPENAI_API_KEY` are environment variables and were never in these models,
    which is why the surface is this small — and why a fourth entry appearing here is worth a
    second look rather than a passing test.
    """
    assert set(SENSITIVE_PATHS) == {'api.tokens.*.token', 'telegram.bot_token', 'telegram.chat_id'}


@pytest.mark.parametrize('root', _ROOTS)
def test_the_sweep_actually_walks_something(root):
    """A walk that silently found nothing would make every assertion above vacuous."""
    assert len(_string_leaves(root)) >= 5
