PYTHON ?= python3
VENV := .venv
PY := $(VENV)/bin/python

.PHONY: test coverage venv

$(PY): requirements-dev.txt
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install -q -r requirements-dev.txt
	@touch $(PY)

venv: $(PY)

test: $(PY)
	$(PY) -m pytest

coverage: $(PY)
	$(PY) -m pytest --cov --cov-report=term-missing
