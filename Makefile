PYTHON ?= python3
VENV := .venv
PY := $(VENV)/bin/python
STAMP := $(VENV)/.installed

.PHONY: test coverage venv

$(STAMP): requirements.txt requirements-dev.txt
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install -q -r requirements-dev.txt
	$(PY) -m playwright install chromium
	@touch $(STAMP)

venv: $(STAMP)

test: $(STAMP)
	$(PY) -m pytest

coverage: $(STAMP)
	$(PY) -m pytest --cov --cov-report=term-missing
