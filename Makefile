.DEFAULT_GOAL := help
.PHONY: help install install-locked test test-cpp lint format bench bench-ci placebo injection \
	reproduce clean

PYTHON_SOURCES := python tests benchmarks

help: ## List the available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Editable install with dev tools, from the ranges in pyproject.toml
	pip install -e ".[dev]"

install-locked: ## Editable install from the exact pins CI uses
	pip install -r requirements-dev.lock
	pip install -e ".[dev]"

test: ## Python suite; needs no external data
	pytest -v

test-cpp: ## C++ engine tests (CMake + Catch2)
	cmake -B build/cpp -DSHADOWFILL_BUILD_CORE=ON -DSHADOWFILL_BUILD_TESTS=ON -DSHADOWFILL_BUILD_BINDINGS=OFF
	cmake --build build/cpp -j
	ctest --test-dir build/cpp --output-on-failure

lint: ## ruff check, ruff format --check, mypy --strict
	ruff check $(PYTHON_SOURCES)
	ruff format --check $(PYTHON_SOURCES)
	mypy

format: ## Apply ruff fixes and formatting in place
	ruff check --fix $(PYTHON_SOURCES)
	ruff format $(PYTHON_SOURCES)

bench: ## Throughput gate: absolute events/s, for developer hardware
	python benchmarks/bench_replay.py

bench-ci: ## Throughput gate: speedup over the reference engine, for shared runners
	python benchmarks/bench_replay.py --ci

placebo: ## The falsification test every result depends on
	pytest tests/python/test_placebo.py -v

injection: ## Known-bias injection: a planted bias must be found
	pytest tests/python/test_known_bias_injection.py -v

reproduce: test test-cpp bench placebo injection ## Every gate, then the synthetic ground-truth run
	python -m shadowfill.ground_truth --config configs/ground_truth_synthetic.yaml

clean: ## Remove build outputs and tool caches
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache
