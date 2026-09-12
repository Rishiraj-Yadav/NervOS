.PHONY: bootstrap dev-api dev-web test test-e2e e2e lint typecheck security check clean-check

PYTHON ?= python

bootstrap:
	$(PYTHON) scripts/bootstrap.py

dev-api:
	uv run python scripts/dev.py api

dev-web:
	uv run python scripts/dev.py web

test:
	uv run python scripts/check.py test

test-e2e:
	uv run python scripts/check.py e2e

e2e: test-e2e

lint:
	uv run python scripts/check.py lint

typecheck:
	uv run python scripts/check.py typecheck

security:
	uv run python scripts/check.py security

check:
	uv run python scripts/check.py check

clean-check:
	uv run python scripts/clean_check.py
