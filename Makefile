PY := .venv/bin/python
HOST ?= 127.0.0.1
PORT ?= 8000

.PHONY: help setup setup-live init doctor create-admin test lint seed-demo sample-videos run run-mock run-pilot backup purge

help:
	@echo "First time:  make setup-live && make init && (edit .env: GEMINI_API_KEY) && make create-admin && make doctor"
	@echo "Daily:       make run      (open http://127.0.0.1:8000)"
	@echo "Other:       make test | make backup | make purge | make run-mock (dummy data only)"

setup:
	python3.13 -m venv .venv || python3 -m venv .venv
	$(PY) -m pip install -e ".[dev]"

setup-live:
	python3.13 -m venv .venv || python3 -m venv .venv
	$(PY) -m pip install -e ".[dev,live]"

init:
	$(PY) scripts/init_env.py

doctor:
	$(PY) scripts/doctor.py

create-admin:
	@read -p "Admin email: " e; $(PY) scripts/create_user.py $$e admin

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check .

seed-demo:
	MOCK_GEMINI=true $(PY) scripts/seed_demo.py

sample-videos:
	$(PY) scripts/make_dummy_video.py sample_videos

# Real assessments (reads .env; needs GEMINI_API_KEY). Bound to localhost.
run run-pilot:
	MOCK_GEMINI=false $(PY) -m uvicorn app.web:get_app --factory --host $(HOST) --port $(PORT) --timeout-keep-alive 75

# Offline demo with dummy scores. Never use for real recordings.
run-mock:
	MOCK_GEMINI=true $(PY) -m uvicorn app.web:get_app --factory --host $(HOST) --port $(PORT)

backup:
	$(PY) scripts/backup.py

purge:
	$(PY) scripts/purge_expired.py
