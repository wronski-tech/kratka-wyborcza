# Local checks (default test suite does not require PostgreSQL).
.PHONY: test test-integration test-mart compile lint

# Tests that need a built mart (scripts/build_mart.py); CI runs them after building it.
MART_TESTS = tests.test_e2e_stubbed_llm

PYTHON ?= python3

test:
	PYTHONPATH=. $(PYTHON) -m unittest discover -s tests -p 'test*.py' -v

test-integration:
	PLUJKA_RUN_DB_TESTS=1 PYTHONPATH=. $(PYTHON) -m unittest discover -s tests -p 'test*.py' -v

test-mart:
	PLUJKA_RUN_MART_TESTS=1 PYTHONPATH=. $(PYTHON) -m unittest -v $(MART_TESTS)

compile:
	$(PYTHON) -m compileall -q api scripts streamlit_app tests

lint:
	$(PYTHON) -m ruff check api scripts streamlit_app tests
	$(PYTHON) -m mypy
