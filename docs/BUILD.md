# Сборка standalone-релиза (PyInstaller)

## Главное правило

**PyInstaller не умеет кросс-компиляцию.** Windows-`.exe` собирается **только на
Windows**, macOS-бинарник — только на macOS. Поэтому:

| Где собрать | Чем | Что получится |
|-------------|-----|---------------|
| macOS (для проверки) | `./build_mac.sh` | `dist/HomeCamera/HomeCamera` (macOS) |
| Windows (для раздачи) | `build.bat` | `dist\HomeCamera\HomeCamera.exe` |
| GitHub (без своего ПК) | Actions → `build-windows` | ZIP-артефакт для Windows |

---

## Сборка и проверка на macOS

```bash
cd security_camera
./build_mac.sh                     # installer pyinstaller + сборка (--onedir)
```

Что проверить (логика frozen — пути через `sys._MEIPASS`, поиск ffmpeg/моделей):

```bash
dist/HomeCamera/HomeCamera enroll          # распознать лица из known/
dist/HomeCamera/HomeCamera --preview       # окно превью
dist/HomeCamera/HomeCamera --source clip.mp4   # тест на файле, без камеры
dist/HomeCamera/HomeCamera selftest        # проверка Telegram
dist/HomeCamera/HomeCamera camwatch --once # кто ещё держит камеру (Windows)
dist/HomeCamera/HomeCamera camwatch --parallel  # делится ли камера с другими приложениями
dist/HomeCamera/HomeCamera --version       # версия сборки
```

Важно: рядом с бинарником должен лежать `config.yaml` (создаётся скриптом из
`config.example.yaml`). Изменяемые данные (`config.yaml`, `known/`, `recordings/`,
`logs/`) пишутся **рядом с exe**, а read-only ресурсы (`config.example.yaml`,
`yolov8n.pt`) — читаются из бандла.

> Сборка тяжёлая: торч + onnxruntime ≈ несколько ГБ и несколько минут.

---

## Сборка Windows-релиза

На Windows (Python 3.10+ установлен, включён в PATH):

```bat
build.bat          :: venv + зависимости + pyinstaller + сборка dist\HomeCamera\
PACK_RELEASE.bat   :: упаковать в dist\HomeCamera_v2.1_Windows.zip
```

`ffmpeg.exe` **вшит в сборку** (пакет `imageio-ffmpeg`): вручную ничего скачивать не
нужно. Свой `ffmpeg.exe` рядом с `build.bat` по-прежнему имеет приоритет (быстрее
стартует), но без него всё работает.

---

## Сборка в облаке (GitHub Actions, без своего Windows)

В репозитории есть `.github/workflows/build-windows.yml`. Он сам:
ставит Python, зависимости, качает `yolov8n.pt` и `ffmpeg.exe`, собирает
`HomeCamera.spec` и выкладывает артефакт `HomeCamera_v2.0_Windows`.

Запуск: **Actions → build-windows → Run workflow** (или просто пуш в `main`, или тег `v2.1`).
Робот собирает приложение и пакует **`HomeCamera_v2.1_Windows.zip`**:

- артефакт `HomeCamera_v2.1_Windows` — скачивается со страницы запуска;
- на теге `v*` ZIP дополнительно прикрепляется к **GitHub Release** (прямая ссылка).

Внутри ZIP (после распаковки — папка `HomeCamera\`):
`HomeCamera.exe` + `_internal\` (все библиотеки) + `ffmpeg.exe` + `known\` (пустая) +
`config.yaml` (шаблон) + `START_CAMERA.bat` + `INSTALL_AUTOSTART.bat` + `UNINSTALL.bat` +
`download_models.bat` + `README.md`.

---

## Что внутри сборки

```
HomeCamera/
├── HomeCamera.exe         запуск (без окна консоли)
├── _internal/             библиотеки (PyInstaller 6)
├── config.example.yaml    шаблон настроек
├── config.yaml            ← появится при первом запуске (тут токен бота)
├── known/                 сюда фото лиц
├── ffmpeg.exe             (Windows)
└── *.bat                  START_CAMERA / INSTALL_AUTOSTART / UNINSTALL / download_models
```

## Ручной запуск из Python (без сборки)

```bash
python launcher.py            # камера
python launcher.py enroll     # лица
python launcher.py selftest   # Telegram
```
