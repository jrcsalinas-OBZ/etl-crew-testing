PY ?= python
ifeq ($(OS),Windows_NT)
  VENV_BIN := .venv/Scripts
else
  VENV_BIN := .venv/bin
endif

.PHONY: install dev backend frontend test seed

install:
	cd backend && $(PY) -m venv .venv && $(VENV_BIN)/python -m pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && $(VENV_BIN)/python -m uvicorn app.main:create_app --factory --reload --port 8000

frontend:
	cd frontend && npm run dev

dev:
	$(MAKE) -j2 backend frontend

test:
	cd backend && $(VENV_BIN)/python -m pytest -q
	cd frontend && npm run typecheck

seed:
	cd backend && $(VENV_BIN)/python -m app.seed
