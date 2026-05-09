VENV := venv
PYTHON_BIN ?= python3.12
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: install lock test clean

install:
	$(PYTHON_BIN) -m venv $(VENV)
	$(PIP) install --upgrade pip
	@if [ -f requirements.lock.txt ]; then \
		echo "Installing from requirements.lock.txt (pinned)"; \
		$(PIP) install -r requirements.lock.txt; \
	else \
		echo "No lockfile — installing from requirements.txt and downloading spaCy model"; \
		$(PIP) install -r requirements.txt; \
		$(PYTHON) -m spacy download en_core_web_trf; \
	fi

lock:
	$(PIP) install --upgrade -r requirements.txt
	$(PYTHON) -m spacy download en_core_web_trf
	$(PIP) freeze > requirements.lock.txt
	@echo "Lockfile regenerated. Review and commit requirements.lock.txt."

test:
	$(PYTHON) -m pytest

clean:
	rm -rf $(VENV)
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
