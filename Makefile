.PHONY: setup dev test train

setup:
	uv sync

dev:
	uv run uvicorn api.main:app --reload

test:
	uv run pytest

train:
	@echo "No ML artefacts to train yet (lands in T04-T09)."
