.PHONY: install test test-cpp lint bench bench-ci placebo injection reproduce clean

install:
	pip install -e ".[dev]"

test:
	pytest -v

test-cpp:
	cmake -B build/cpp -DSHADOWFILL_BUILD_CORE=ON -DSHADOWFILL_BUILD_TESTS=ON -DSHADOWFILL_BUILD_BINDINGS=OFF
	cmake --build build/cpp -j
	ctest --test-dir build/cpp --output-on-failure

lint:
	ruff check python tests
	ruff format --check python tests
	mypy

bench:
	python benchmarks/bench_replay.py

bench-ci:
	python benchmarks/bench_replay.py --ci

placebo:
	pytest tests/python/test_placebo.py -v

injection:
	pytest tests/python/test_known_bias_injection.py -v

reproduce: test test-cpp bench placebo injection
	python -m shadowfill.ground_truth --config configs/ground_truth_synthetic.yaml

clean:
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache
