.PHONY: help setup run rerun test clean show

VENV := .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

help:
	@echo "make setup   - create .venv and install PySpark + Delta Lake"
	@echo "make run     - generate source data and run the whole lakehouse end to end"
	@echo "make rerun   - run the pipeline again over the same data (proves idempotency)"
	@echo "make test    - run the pytest suite"
	@echo "make show    - print the gold tables from the last run"
	@echo "make clean   - delete all generated data and start fresh"

setup:
	python3 -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -r requirements.txt
	$(PIP) install --quiet -e .
	@echo "Ready. Now run: make run"

run:
	$(PY) run_pipeline.py --fresh

rerun:
	$(PY) run_pipeline.py --batch 2

test:
	$(VENV)/bin/pytest

show:
	$(PY) run_pipeline.py --show-only

clean:
	rm -rf data metastore_db derby.log
	@echo 'Cleaned. "make run" will rebuild everything.'
