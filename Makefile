# Makefile — быстрые команды для локального запуска (macOS / Linux).
#
#   make help      — список команд
#   make run       — запустить камеру (headless)
#   make preview   — запустить с окном превью
#   make test      — прогнать автотесты (этапы 2 и 5)
#   make cpu       — замерить CPU в простое
#   make enroll    — зарегистрировать лица из known/
#   make setup     — создать venv и поставить зависимости

PY := $(shell [ -x ../venv/bin/python ] && echo ../venv/bin/python || \
      ([ -x venv/bin/python ] && echo venv/bin/python || echo python3))

.PHONY: help run preview test selftest cpu enroll setup clean

help:
	@echo "Доступные команды:"
	@echo "  make run       запустить камеру (headless)"
	@echo "  make preview   запустить с окном превью"
	@echo "  make test      автотесты (этапы 2 и 5)"
	@echo "  make selftest  проверить Telegram (фото + видео со звуком)"
	@echo "  make cpu       CPU в простое (30s)"
	@echo "  make enroll    распознать лица из known/"
	@echo "  make setup     создать venv и поставить зависимости"
	@echo "  make clean     удалить временные файлы"

run:
	./run_local.sh

preview:
	./run_local.sh --preview

test:
	PYTHONPATH=. $(PY) tests/test_stage2.py
	PYTHONPATH=. $(PY) tests/test_stage5.py

selftest:
	$(PY) selftest.py

cpu:
	./scripts/idle_cpu.sh

enroll:
	$(PY) enroll.py

setup:
	./setup.sh

clean:
	rm -rf _tmp _audio __pycache__ tests/__pycache__
