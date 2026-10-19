.PHONY: setup dev test train db catalogue web data

db:
	docker compose up -d db

catalogue:
	uv run python -m scripts.fetch_catalogue

setup:
	uv sync

dev:
	uv run uvicorn api.main:app --reload

test:
	uv run pytest

train:
	uv run python -m ml.train_risk

web:
	cd web && npm run dev

data:
	uv run python -m ml.data.download_olist
	uv run python -m ml.build_risk_dataset
