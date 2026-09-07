.PHONY: bootstrap dev-api dev-web test lint typecheck check

PYTHON ?= python

bootstrap:
	$(PYTHON) scripts/bootstrap.py

dev-api:
	uv run python scripts/dev.py api

dev-web:
	uv run python scripts/dev.py web

test:
	uv run python scripts/check.py test

lint:
	uv run python scripts/check.py lint

typecheck:
	uv run python scripts/check.py typecheck

check:
	uv run python scripts/check.py check
