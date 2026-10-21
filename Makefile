PY := .venv/bin/python
TORCH_CPU := https://download.pytorch.org/whl/cpu

.PHONY: setup check lint test test-slow ui ui-envcheck

setup:
	python3 -m venv .venv
	$(PY) -m pip install -q --index-url $(TORCH_CPU) torch==2.14.1
	$(PY) -m pip install -q -e '.[dev]'

lint:
	$(PY) -m ruff check .

# Определение готовности любой задачи. pytest exit 5 = «тестов ещё нет», это не ошибка.
check: lint
	$(PY) -m pytest -q -m "not slow"; rc=$$?; [ $$rc -eq 0 ] || [ $$rc -eq 5 ]

test-slow:
	$(PY) -m pytest -q -m slow; rc=$$?; [ $$rc -eq 0 ] || [ $$rc -eq 5 ]

test:
	$(PY) -m pytest -q; rc=$$?; [ $$rc -eq 0 ] || [ $$rc -eq 5 ]

# Интерфейс (U1): один сервер на продукт, окно Edge InPrivate; повторный запуск открывает ещё окно.
ui:
	$(PY) -m voxcut.ui.launch

# S9: проверки окружения при работающем make ui (вне make check).
ui-envcheck:
	$(PY) -m voxcut.ui.envcheck
