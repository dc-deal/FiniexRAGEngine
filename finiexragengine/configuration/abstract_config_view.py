"""The one place a configuration object becomes publishable JSON (2026-09-08).

`GET /v1/configs/{name}` serves the effective configuration of this process so a remote diagnosis
can read what the machine actually runs — the `user_configs/` overlay included, which is the layer
nothing exposed before. That means handing config objects to a public surface, and config objects
hold credentials.

So the projection is **not** the router's job and not a helper the router may forget to call. A
concrete view supplies *which* documents it publishes and *where its override entries come from*;
this base class owns turning them into JSON, and there is no other path. A later caller that wants
configuration over some other transport inherits the policy instead of re-deciding it — which is the
failure this shape prevents, because re-deciding it is how the second implementation leaks.

Two layers guard every string, and they answer different questions:

- **the path policy** (`config_redaction.py`) — is this *field* a secret? Decided per field, written
  down, and enforced by a contract test that fails when a model grows a string nobody classified;
- **the pattern scrubber** (`finiex_auth.redaction`) — does this *value* look like a credential? The
  same vocabulary the log route uses, for the case the first layer cannot foresee: a feed URL
  carrying its own API key in a field that is legitimately public.

Everything either layer touches is named in the answer. A withheld value is honest; a silently
altered one is not, and the reader cannot tell the difference without being told.
"""
import re
from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel
from finiex_auth.redaction import MASK, redact

from finiexragengine.configuration.config_redaction import classify
from finiexragengine.configuration.override_report import OverrideEntry
from finiexragengine.types.config_view_types import ConfigDocument, OverrideLeaf

# `sources[fxstreet].enabled` -> `sources.fxstreet.enabled`. The override report anchors patched
# list items by id, the redaction policy addresses segments — one grammar has to give, and it is
# cheaper to translate here than to teach the policy a second notation.
_BRACKET = re.compile(r'\[([^\]]*)\]')


class AbstractConfigView(ABC):
    """One config domain, projected for publication.

    Subclasses declare their identity and hand over the objects **this process holds** — never a
    fresh read from disk. A file edited after boot must not make this surface disagree with the
    running engine, for the same reason `/v1/build` samples its commit once at startup.
    """

    NAME: str = ''
    SUMMARY: str = ''
    LAYERS: Tuple[str, ...] = ()

    @abstractmethod
    def documents(self) -> Dict[str, BaseModel]:
        """The effective config objects, keyed by the id a caller would narrow to."""

    @abstractmethod
    def override_entries(self) -> Dict[str, List[OverrideEntry]]:
        """What the gitignored overlay moved, keyed the same way. Empty when it moved nothing."""

    def layers(self) -> List[str]:
        """The files or directories this document was merged from, tracked layer first.

        A method rather than only the class constant, because one view knows its layers as
        directories declared up front and another resolves them per process (a `user_configs/`
        file that does not exist should not be listed as if it did).
        """
        return list(self.LAYERS)

    def render(self, doc_id: Optional[str] = None) -> Optional[ConfigDocument]:
        """The published document — `None` when `doc_id` names something this view does not have.

        `None` rather than an empty result: "no such pipeline" and "a pipeline with nothing in it"
        are different answers, and the route turns the first into a 404.
        """
        models = self.documents()
        if doc_id is not None:
            if doc_id not in models:
                return None
            models = {doc_id: models[doc_id]}
        document = ConfigDocument(name=self.NAME, summary=self.SUMMARY, layers=self.layers())
        entries = self.override_entries()
        for key, model in models.items():
            # `mode='python'` on purpose: the JSON mode would render a `date` as a string, and this
            # walk classifies strings. A date would then arrive as an unclassified secret.
            document.documents[key] = self._project(model.model_dump(), '', document)
            if key in entries:
                document.overrides[key] = [self._leaf(entry, document) for entry in entries[key]]
        # One path can be masked twice — once in the document, once in the override that set it —
        # and a census that counted it twice would read like two findings.
        for census in (document.redacted, document.unclassified, document.scrubbed):
            census[:] = list(dict.fromkeys(census))
        return document

    def _project(self, value: Any, path: str, census: ConfigDocument) -> Any:
        """Walk one config value, masking as the policy says and recording every change."""
        if isinstance(value, dict):
            return {key: self._project(item, self._join(path, str(key)), census)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [self._project(item, self._join(path, str(index)), census)
                    for index, item in enumerate(value)]
        if isinstance(value, str):
            return self._project_string(value, path, census)
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if value is None or isinstance(value, (bool, int, float)):
            # Numbers and flags pass through unlisted: a threshold IS the diagnostic payload, and
            # requiring a signature for every int would make the policy noise nobody reads.
            return value
        # Anything else is a shape nobody decided how to publish — a surviving model, a Path, an
        # engine object. Raising here is the `to_jsonable` discipline: a defect the suite sees,
        # rather than an engine's internals appearing on the wire because a fallback stringified it.
        raise TypeError(f'{path or "<root>"}: cannot publish {type(value).__name__}')

    def _project_string(self, value: str, path: str, census: ConfigDocument) -> str:
        """Layer 1 by path, then layer 2 by pattern — and the answer names whichever fired."""
        if not value:
            # An empty string is not a credential, and masking it claims one exists. That is not
            # pedantry: `telegram.bot_token: «redacted»` next to `enabled: false` reads as "a token
            # you may not see", when the answer to "is Telegram configured on this machine" is
            # simply no — a question this surface exists to answer.
            return value
        verdict = classify(path)
        if verdict == 'sensitive':
            census.redacted.append(path)
            return MASK
        if verdict == 'unclassified':
            # Masked like a secret, reported unlike one: this says the policy is stale, not that the
            # field is dangerous. The contract test makes the state short-lived.
            census.unclassified.append(path)
            return MASK
        scrubbed, changed = redact(value)
        if changed:
            census.scrubbed.append(path)
        return scrubbed

    @staticmethod
    def _join(prefix: str, segment: str) -> str:
        return f'{prefix}.{segment}' if prefix else segment

    def _leaf(self, entry: OverrideEntry, census: ConfigDocument) -> OverrideLeaf:
        """An override entry as the API publishes it — projected, never passed through raw.

        This is the leak that would have been easy to miss. `user_configs/app_config.json` is
        precisely the file the bearer tokens and the bot token live in, so an override entry's
        *value* is a secret exactly when the leaf it names is one. Running both values back through
        the same projection means the policy applies once and covers both halves of the answer.

        `added` distinguishes "the tracked file has no such key" from an explicit JSON `null`, and
        that distinction has to survive the wire or the two collapse into one ambiguous `None`.
        """
        path = _BRACKET.sub(r'.\1', entry.path)
        # An `unknown` key names no field in any model, so it can never be classified and no
        # contract test can ever cover it. Its strings are still masked — a key misfiled by hand is
        # exactly where a secret ends up by accident, and production had a real one within an hour
        # of this shipping (`weekly_report.report_command`, which belongs on `telegram`) — but the
        # census must not record it: `unclassified` has to keep meaning "the policy is stale", or a
        # reader chases a red test that is green. The `unknown` flag below is the honest signal.
        into = census if not entry.unknown else ConfigDocument(name='', summary='')
        return OverrideLeaf(
            path=entry.path,                                   # as the boot line spells it
            value=self._project(entry.override_value, path, into),
            was=None if entry.added else self._project(entry.base_value, path, into),
            added=entry.added, unknown=entry.unknown)
