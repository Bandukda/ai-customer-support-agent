PYTHON ?= python3.12

.PHONY: setup dev dev-noopen test lint

## Create the virtualenv and install dependencies
setup:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r backend/requirements.txt

## Run the app (API + frontend) at http://localhost:8000 and open a browser
dev:
	@( for i in $$(seq 1 60); do \
	     curl -sf http://localhost:8000/api/health >/dev/null 2>&1 && { \
	       open -a "Google Chrome" http://localhost:8000 2>/dev/null \
	         || open http://localhost:8000; break; }; \
	     sleep 0.3; \
	   done ) &
	.venv/bin/uvicorn app.main:app --reload --app-dir backend --port 8000

## Same, without opening a browser (use during recording)
dev-noopen:
	.venv/bin/uvicorn app.main:app --reload --app-dir backend --port 8000

## Run the test suite
test:
	.venv/bin/python -m pytest backend/tests -v

## Check lint & import order (add --fix to apply)
lint:
	.venv/bin/ruff check backend/
