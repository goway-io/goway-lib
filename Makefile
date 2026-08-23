lint:
	uv run ruff check src tests --fix && uv run ruff format src tests

test:
	uv run pytest -q
