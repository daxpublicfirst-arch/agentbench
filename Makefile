.PHONY: install test lint self-test doctor build dist repro-check clean check

PYTHON ?= python3
PIP ?= $(PYTHON) -m pip
# Reproducible builds: the epoch is the head commit's timestamp, so a
# rebuild of the same source gives byte-identical artifacts.
EPOCH ?= $(shell git log -1 --format=%ct 2>/dev/null || echo 0)

install:
	$(PIP) install -e ".[dev]"

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m compileall -q agentbench tests
	$(PYTHON) -m ruff check agentbench tests
	$(PYTHON) -m ruff format --check agentbench tests

self-test:
	agentbench self-test

doctor:
	agentbench doctor

build:
	SOURCE_DATE_EPOCH=$(EPOCH) PYTHONHASHSEED=0 $(PYTHON) -m build

dist: clean build
	sha256sum dist/* > dist/SHA256SUMS
	cat dist/SHA256SUMS

# Build twice into temp dirs and require byte-identical artifacts.
repro-check:
	rm -rf /tmp/agentbench-repro-a /tmp/agentbench-repro-b
	SOURCE_DATE_EPOCH=$(EPOCH) PYTHONHASHSEED=0 $(PYTHON) -m build --outdir /tmp/agentbench-repro-a
	SOURCE_DATE_EPOCH=$(EPOCH) PYTHONHASHSEED=0 $(PYTHON) -m build --outdir /tmp/agentbench-repro-b
	sha256sum /tmp/agentbench-repro-a/* /tmp/agentbench-repro-b/*
	diff <(sha256sum /tmp/agentbench-repro-a/* | awk '{print $$1}') \
	     <(sha256sum /tmp/agentbench-repro-b/* | awk '{print $$1}') \
	     && echo "reproducible: wheel and sdist are byte-identical across builds"

clean:
	rm -rf dist build *.egg-info agentbench.egg-info .pytest_cache
	rm -rf /tmp/agentbench-repro-a /tmp/agentbench-repro-b
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

check: lint test self-test doctor
