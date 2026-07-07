# Contributing

Thanks for your interest in FiniexRAGEngine.

## Development setup

```bash
cp .env.example .env          # set OPENAI_API_KEY
docker compose up -d          # engine + pgvector PostgreSQL
pytest tests/ -v
```

A VS Code dev container (`.devcontainer/`) and launch configurations (`.vscode/launch.json`)
are included.

## Conventions

- **Fully typed.** Runtime domain types use `@dataclass`; config schemas use Pydantic models.
- **String literals use single quotes**; double quotes only for f-strings and docstrings.
- **Imports at the top** of each file, grouped standard library → third party → project.
- **No `__init__.py`** — the project uses fully-qualified import paths from the package root.
- **One class per file**; the file name matches the class in snake_case.
- All datetimes are timezone-aware **UTC**.

## Pull requests

- Keep changes focused and add tests for new behavior.
- Run `pytest tests/ -v` before opening a PR.
- Describe what changed and why.
