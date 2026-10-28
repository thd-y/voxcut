PY := .venv/bin/python
TORCH_CPU := https://download.pytorch.org/whl/cpu

.PHONY: setup check lint test test-ci test-slow ui ui-envcheck hooks

setup:
	python3 -m venv .venv
	$(PY) -m pip install -q --index-url $(TORCH_CPU) torch==2.14.1
	$(PY) -m pip install -q -e '.[dev]'

lint:
	$(PY) -m ruff check .

# The definition of done for every task. pytest exit 5 means "no tests yet", not an error.
check: lint
	$(PY) -m pytest -q -m "not slow"; rc=$$?; [ $$rc -eq 0 ] || [ $$rc -eq 5 ]
	@if command -v node >/dev/null 2>&1; then node --test tests/js/*.test.js; \
	else echo "WARNING: node not found, JS tests skipped (D48)"; fi

test-slow:
	$(PY) -m pytest -q -m slow; rc=$$?; [ $$rc -eq 0 ] || [ $$rc -eq 5 ]

test:
	$(PY) -m pytest -q; rc=$$?; [ $$rc -eq 0 ] || [ $$rc -eq 5 ]

# CI (D63): everything except tests with real systemd --user units.
test-ci:
	$(PY) -m pytest -q -m "not host"; rc=$$?; [ $$rc -eq 0 ] || [ $$rc -eq 5 ]

# The interface (U1): one server per product, an Edge InPrivate window; a second launch opens another window.
ui:
	$(PY) -m voxcut.ui.launch

# S9: environment checks while make ui is running (outside make check).
ui-envcheck:
	$(PY) -m voxcut.ui.envcheck

# D128: leak check on commit, commit message and push.
hooks:
	git config core.hooksPath .githooks
