# garmin-reasoner

Personal service that receives Garmin Connect data via webhooks, stores it as raw JSON in
SQLite, and exposes it to Claude through a remote MCP server. See [CLAUDE.md](CLAUDE.md) for
the architecture and build plan.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```sh
cp .env.example .env    # fill in values
uv sync                 # install dependencies
uv run pytest           # run tests
uv run ruff check .     # lint
uv run uvicorn garmin_reasoner.main:create_app --factory --reload
```
