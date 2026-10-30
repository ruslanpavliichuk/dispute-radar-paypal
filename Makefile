.PHONY: setup dev test train db catalogue web data disputes claims

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
	uv run python -m ml.train_risk_final
	uv run python -m ml.train_outcome
	uv run python -m ml.train_claims

web:
	cd web && npm run dev

disputes:
	uv run python -m ml.generate_disputes

data:
	uv run python -m ml.data.download_olist
	uv run python -m ml.build_risk_dataset

claims:
	uv run python -m ml.data.download_cfpb
	uv run python -m ml.label_claims
