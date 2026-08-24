.PHONY: dev test

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

dev:
	cd backend && uvicorn app.main:create_app --factory --reload --host 0.0.0.0 --port 8000

test:
	PYTHONPATH=backend $(PYTHON) -m pytest tests
