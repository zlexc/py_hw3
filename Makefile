UV ?= uv

.PHONY: install run lint format format-check check build

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

check: lint format-check

build:
	$(UV) build
