UV ?= uv

.PHONY: install run lint format format-check test check build

install:
	$(UV) sync --locked

run:
	$(UV) run --locked wallet

lint:
	$(UV) run --locked ruff check .

format:
	$(UV) run --locked ruff format .

format-check:
	$(UV) run --locked ruff format --check .

test:
	$(UV) run --locked python -m unittest discover -s tests -t . -v

check: lint format-check test

build:
	$(UV) build
