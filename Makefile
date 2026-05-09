VENV := venv
PYTHON_BIN ?= python3.12
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: install test clean

install:
	$(PYTHON_BIN) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	$(PYTHON) -m spacy download en_core_web_trf

test:
	$(PYTHON) -m pytest

clean:
	rm -rf $(VENV)
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
