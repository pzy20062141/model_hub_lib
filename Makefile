.PHONY: install test openapi build

install:
	uv sync --extra api --extra test

test:
	uv run pytest

openapi:
	uv run python scripts/export_openapi.py

build:
	uv build

