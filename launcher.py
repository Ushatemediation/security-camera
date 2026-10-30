#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Единая точка входа для собранного HomeCamera.exe (и для запуска из исходников).

Использование:
    HomeCamera.exe              запуск камеры (headless)
    HomeCamera.exe --preview    с окном превью
    HomeCamera.exe enroll       регистрация лиц из known/
    HomeCamera.exe selftest     проверка Telegram (фото + видео со звуком)
"""

import sys

# В сборке без консоли (console=False) sys.stdout/stderr равны None,
# и библиотеки с прогресс-барами (tqdm в insightface) падают с AttributeError.
# Подменяем их на пустой поток — тогда загрузка моделей не ломается.
if sys.stdout is None or sys.stderr is None:
    import io

    class _Null(io.TextIOBase):
        def write(self, s):  # noqa: D401 - заглушка
            return len(s)

        def flush(self):
            pass

        def isatty(self):
            return False

    if sys.stdout is None:
        sys.stdout = _Null()
    if sys.stderr is None:
        sys.stderr = _Null()


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "run"

    if cmd == "enroll":
        sys.argv = [sys.argv[0], *args[1:]]
        import enroll
        enroll.main()
        return 0

    if cmd == "selftest":
        sys.argv = [sys.argv[0], *args[1:]]
        import selftest
        return selftest.main()

    # всё остальное — запуск камеры (main.py разберёт --preview/--camera/--source)
    sys.argv = [sys.argv[0], *args]
    import main
    main.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
