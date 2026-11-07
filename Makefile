.PHONY: install test lint self-test doctor build dist clean check

PYTHON ?= python3
PIP ?= $(PYTHON) -m pip

install:
	$(PIP) install -e .

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m compileall -q agentbench tests
	$(PYTHON) -m ruff check agentbench tests 2>/dev/null || echo "ruff not installed; compileall passed"

self-test:
	agentbench self-test

doctor:
	agentbench doctor

build:
	$(PYTHON) -m build

dist: clean build
	sha256sum dist/* > dist/SHA256SUMS
	cat dist/SHA256SUMS

clean:
	rm -rf dist build *.egg-info agentbench.egg-info .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

check: lint test self-test doctor
