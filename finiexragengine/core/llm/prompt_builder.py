"""Prompt builder — renders a versioned Jinja2/Markdown template with symbol + context (ISSUE_6)."""
from pathlib import Path
from typing import List

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

from finiexragengine.exceptions.ragengine_errors import LLMError
from finiexragengine.types.article_types import Article


class PromptBuilder:
    """Renders per-symbol prompts from versioned Jinja2 Markdown templates under `prompts/`.

    Templates are `prompts/<name>_v<prompt_version>.md`, selected by the constellation's
    `prompt_version` — prompts *and* their article-rendering loop stay in one reviewable
    file, out of Python; a bumped version is a new file (clean replay/backfill). The
    template receives `symbol` and the retrieved `articles`; the LLM scores only the mood —
    provenance is attached downstream from the real articles, never invented by the model.
    """

    def __init__(self, prompts_dir: Path) -> None:
        # autoescape off — this is raw prompt text, not HTML. StrictUndefined — a typo'd
        # variable fails loudly instead of rendering empty. trim/lstrip — `{% %}` control
        # lines leave no stray blank lines in the rendered prompt.
        self._env = Environment(
            loader=FileSystemLoader(str(prompts_dir)),
            autoescape=False,
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def build(self, name: str, prompt_version: str, symbol: str,
              articles: List[Article]) -> str:
        """Render the `<name>_v<prompt_version>.md` template for `symbol` + its context."""
        template_name = f'{name}_v{prompt_version}.md'
        try:
            template = self._env.get_template(template_name)
        except TemplateNotFound as exc:
            raise LLMError(f'prompt template not found: {template_name}') from exc
        return template.render(symbol=symbol, articles=articles)
