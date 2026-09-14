.PHONY: bootstrap up down ps logs lint typecheck test test-backend test-frontend e2e smoke build doctor
BASE_URL ?= http://localhost:3000
bootstrap:
	@test -f .env || (echo 'Copia .env.example a .env y define POSTGRES_PASSWORD.'; exit 1)
	docker compose up --build -d --wait
up:
	docker compose up -d --wait
down:
	docker compose down
ps:
	docker compose ps
logs:
	docker compose logs --tail=100 -f
lint:
	cd backend && uv run ruff check src tests ../scripts
	cd frontend && pnpm lint
typecheck:
	cd backend && uv run mypy src/trackvance --check-untyped-defs --ignore-missing-imports
	cd frontend && pnpm typecheck
test: test-backend test-frontend
test-backend:
	cd backend && uv run pytest tests ../scripts/tests
test-frontend:
	cd frontend && pnpm test
e2e:
	cd frontend && PLAYWRIGHT_BASE_URL=$(BASE_URL) pnpm test:e2e
smoke:
	python scripts/smoke_test.py --base-url $(BASE_URL)
build:
	cd frontend && pnpm build
doctor:
	python scripts/doctor.py --base-url $(BASE_URL) --docker
