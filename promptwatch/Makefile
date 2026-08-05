.PHONY: up down dev test migrate seed install-sdk

up:
	docker-compose up -d

down:
	docker-compose down

dev:
	docker-compose up --build

logs:
	docker-compose logs -f api

migrate:
	docker-compose exec api alembic upgrade head

seed:
	docker-compose exec api python scripts/seed.py

test:
	cd api && pytest tests/ -v

test-sdk:
	cd sdk && pytest tests/ -v

build-sdk:
	cd sdk && pip install build && python -m build

install-sdk:
	pip install -e sdk/

shell:
	docker-compose exec api python

psql:
	docker-compose exec db psql -U promptwatch -d promptwatch
