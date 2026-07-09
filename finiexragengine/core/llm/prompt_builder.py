"""Prompt builder — renders a versioned Jinja2/Markdown template with symbol + context
(ISSUE_6) and carries its front-matter identity for reproducibility (ISSUE_33)."""
import hashlib
from pathlib import Path
from typing import Dict, List, Tuple

from jinja2 import Environment, StrictUndefined, Template

from finiexragengine.exceptions.ragengine_errors import LLMError
from finiexragengine.types.article_types import Article
from finiexragengine.types.prompt_metadata import PromptMetadata


def _split_front_matter(text: str) -> Tuple[Dict[str, str], str]:
    """Split a leading `---` front-matter block from the template body.

    Returns (metadata, body). Supports only flat `key: value` scalars — enough for prompt
    metadata (id/version/author/created/description); no nesting, no lists, no deps. A
    template without a fenced block yields ({}, text) so un-annotated prompts still render.
    Splitting on '\\n' is lossless, so the returned body hashes identically to the file's
    post-front-matter content.
    """
    lines = text.split('\n')
    # A front-matter block must open with a line that is exactly '---'.
    if lines[0].strip() != '---':
        return {}, text
    # Find the closing fence; without one, treat the whole file as body (be forgiving).
    closing = None
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            closing = i
            break
    if closing is None:
        return {}, text
    meta: Dict[str, str] = {}
    for line in lines[1:closing]:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):          # blank / comment line
            continue
        key, sep, value = line.partition(':')
        if not sep:                                           # not a key: value line
            continue
        meta[key.strip()] = value.strip().strip('"').strip("'")
    body = '\n'.join(lines[closing + 1:])
    # Drop a single blank line that conventionally follows the closing fence.
    if body.startswith('\n'):
        body = body[1:]
    return meta, body


class PromptBuilder:
    """Renders per-symbol prompts from versioned Jinja2/Markdown templates under `prompts/`.

    Templates are `prompts/<name>_v<version>.md`, selected by the pipeline's declared prompt
    (ISSUE_33) — prompt *and* its article-rendering loop stay in one reviewable file, out of
    Python; a bumped version is a new file (clean replay/backfill). Each file carries a `---`
    front-matter block (id/version/author/created/description); the builder parses it into
    PromptMetadata and fingerprints the body, then renders the body only — the metadata block
    never leaks into the prompt. Parsed templates are cached: a prompt is immutable per version.
    The rendered template receives `symbol` and the retrieved `articles`; the LLM scores only
    the mood — provenance is attached downstream from the real articles, never invented.
    """

    def __init__(self, prompts_dir: Path) -> None:
        self._prompts_dir = Path(prompts_dir)
        # from_string (not FileSystemLoader): we strip the front-matter ourselves before Jinja
        # sees the body. autoescape off — raw prompt text, not HTML. StrictUndefined — a typo'd
        # variable fails loudly. trim/lstrip — `{% %}` control lines leave no stray blank lines.
        self._env = Environment(
            autoescape=False,
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._cache: Dict[Tuple[str, str], Tuple[PromptMetadata, Template]] = {}

    def _load(self, name: str, version: str) -> Tuple[PromptMetadata, Template]:
        """Read, parse (front-matter -> metadata) and compile `<name>_v<version>.md`, cached."""
        key = (name, version)
        if key in self._cache:
            return self._cache[key]
        path = self._prompts_dir / f'{name}_v{version}.md'
        try:
            raw = path.read_text(encoding='utf-8')
        except FileNotFoundError as exc:
            raise LLMError(f'prompt template not found: {path.name}') from exc
        meta, body = _split_front_matter(raw)
        # Fingerprint the body (front-matter excluded): a silent edit shows in the output;
        # a metadata-only fix does not invalidate the series. Short git-style hex is enough.
        content_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()[:12]
        metadata = PromptMetadata(
            id=meta.get('id', name),                  # fall back to the file name if unset
            version=str(meta.get('version', version)),
            author=meta.get('author', ''),
            created=meta.get('created', ''),
            description=meta.get('description', ''),
            content_hash=content_hash,
        )
        compiled = self._env.from_string(body)
        self._cache[key] = (metadata, compiled)
        return metadata, compiled

    def metadata(self, name: str, version: str) -> PromptMetadata:
        """The prompt's front-matter identity (id/version/hash) — for the outcome record."""
        return self._load(name, version)[0]

    def build(self, name: str, prompt_version: str, symbol: str,
              articles: List[Article]) -> str:
        """Render the `<name>_v<prompt_version>.md` body for `symbol` + its context."""
        _, compiled = self._load(name, prompt_version)
        return compiled.render(symbol=symbol, articles=articles)
