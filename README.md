# Security Camera 🎥

Охранная камера на домашнем компьютере: следит за кадром, **узнаёт людей в лицо**,
пишет **видео со звуком** и присылает его в **Telegram** (сначала фото, затем клип).

Есть **готовый архив для Windows — ставить ничего не нужно** (Python не требуется).

---

## 🟢 Заказчику: как запустить (3 шага)

1. Скачайте **`HomeCamera_Windows.zip`** из раздела **[Releases](../../releases)** и распакуйте в любую папку.
2. Откройте `config.yaml` и впишите **токен бота** и **chat_id** (как получить — ниже).
   Положите фотографии людей в папку `known/` (имя файла = имя человека, например `pavel.jpg`).
3. Дважды щёлкните **`START_CAMERA.bat`** — программа работает в фоне, окно консоли не появляется.

При появлении человека придёт **фото**, затем **видео со звуком**. Записи — в `recordings/`.

> Внутри архива есть файл **`ПРОЧТИ_МЕНЯ.txt`** — те же шаги, подробнее.
> Автозапуск при включении ПК — `INSTALL_AUTOSTART.bat`. Остановить/удалить — `UNINSTALL.bat`.

### Как получить токен бота (2 минуты)
1. В Telegram: **[@BotFather](https://t.me/BotFather)** → `/newbot` → придумайте имя →
   получите **токен** вида `123456789:AA...` → впишите в `config.yaml` (`token`).
2. Откройте **своего** бота и нажмите **Start**.
3. Узнайте свой **chat_id**: напишите **[@userinfobot](https://t.me/userinfobot)** →
   впишите полученное число в `config.yaml` (`chat_ids`).

```yaml
telegram:
  token: "123456789:AA-ваш-токен"
  chat_ids: [123456789]
```

---

## Что умеет

- **Экономит CPU** — тяжёлые нейросети включаются только при движении в кадре.
- **Узнаёт своих** — распознавание лиц (InsightFace): положили фото → знает человека.
- **Пишет со звуком** — клип = 5 секунд до события + 5 после, с дорожкой микрофона.
- **Уведомления в Telegram** — снимок сразу, затем видео со звуком.
- **Автозапуск** (Windows) — фоновая задача `HomeCameraService`.

---

## Настройки (`config.yaml`)

Все параметры — в одном файле, с комментариями (см. `config.example.yaml`):

| Параметр | Что делает |
|----------|-----------|
| `telegram.token`, `telegram.chat_ids` | куда слать уведомления |
| `recording.pre_seconds` / `post_seconds` | сколько секунд писать до/после события |
| `recording.mux_audio` | вшивать ли звук микрофона прямо в mp4 |
| `face.threshold` | строгость распознавания (0.5 по умолчанию) |
| `motion.*` | чувствительность детектора движения |
| `audio.*` | микрофон, частота, буфер |

> `config.yaml` в `.gitignore` — ваши токены не попадут в репозиторий.

---

## 🛠 Для разработчика

### Запуск из исходников (macOS / Linux / Windows)
```bash
git clone https://github.com/Ushatemediation/security-camera.git
cd security-camera

./setup.sh        # macOS/Linux: venv + зависимости + модели + config.yaml
setup.bat         # Windows

# впишите config.yaml, положите фото в known/ (лица распознаются автоматически)
./start.sh        # macOS/Linux,  (Windows: start.bat)
./start.sh --preview
```

### Полезные команды
```bash
make run        # запуск          make test      # автотесты
make preview    # с окном превью  make selftest  # проверка Telegram
make cpu        # CPU в простое   make enroll    # распознать лица из known/
```
Тест без камеры: `./run_local.sh --source путь/к/видео.mp4`

### Сборка standalone-релиза (PyInstaller)
- macOS: `./build_mac.sh` → `dist/HomeCamera/`
- Windows: `build.bat` → `PACK_RELEASE.bat` → `HomeCamera_v1.0_Windows.zip`
- Без своего Windows: **GitHub Actions → `build-windows` → Run workflow** (или пуш тега `v*`) —
  робот соберёт `HomeCamera_Windows.zip` и выложит в артефакт/Release.

Подробности — `docs/BUILD.md`.

### Структура
```
security_camera/
├── main.py             # главный цикл: движение → лицо → запись
├── notifier.py         # Telegram: фото + видео со звуком
├── audio.py paths.py launcher.py
├── enroll.py selftest.py
├── service/            # фон и автозапуск (Windows)
├── known/              # фото лиц (+ embedding.npy)
├── recordings/         # готовые mp4
├── docs/  tests/
└── HomeCamera.spec     # сборка PyInstaller
```

---

## Лицензия

MIT — см. `LICENSE`.
