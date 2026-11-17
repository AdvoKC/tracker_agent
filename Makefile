.PHONY: install check lint fmt typecheck test migrate revision

install:
	uv sync

check: lint typecheck test

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

test:
	uv run pytest

migrate:
	uv run alembic upgrade head

revision:
	@test -n "$(m)" || (echo 'usage: make revision m="message"' && exit 1)
	uv run alembic revision --autogenerate -m "$(m)"
