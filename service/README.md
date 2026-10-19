# HomeCameraService — фоновый автозапуск охранной камеры (Windows)

Тихий запуск приложения `main.py` без окна консоли, с автозапуском
и двумя уровнями перезапуска при падении.

## Состав
| Файл | Назначение |
|------|-----------|
| `supervisor.py` | Держит `main.py` живым, перезапускает при падении, без окна. |
| `install.ps1` | Регистрирует задачу `HomeCameraService` в Планировщике. |
| `build_service.ps1` | Собирает `HomeCameraService.exe` (PyInstaller, `--noconsole`). |
| `camera_service.xml` | Справочное определение задачи для `schtasks` (ручной вариант). |
| `uninstall.ps1` | Удаляет задачу. |

## 1. Как достигается «тихо и без окон»
- **Без консоли:** запуск через `pythonw.exe` (интерпретатор Python без консоли)
  либо через собранный `HomeCameraService.exe` с флагом PyInstaller `--noconsole`.
- **Без окна у дочернего процесса:** супервизор глушит окно флагом
  `CREATE_NO_WINDOW (0x08000000)`.
- **Имя процесса в Диспетчере:** см. раздел 4.
- Рабочий стол/превью не используются — `main.py` в этой сборке headless.

## 2. Быстрая установка (вариант по умолчанию: pythonw + supervisor)
```powershell
# от имени администратора; -ProjectDir по умолчанию = .. (корень проекта)
powershell -ExecutionPolicy Bypass -File install.ps1
# или явно:
powershell -ExecutionPolicy Bypass -File install.ps1 -ProjectDir "C:\HomeCamera"
```
Скрипт сам найдёт `pythonw.exe` (сначала `venv\Scripts\pythonw.exe`, затем в PATH),
зарегистрирует задачу `HomeCameraService` (триггер — загрузка системы, +30 с) и
запустит её.

## 3. Имя процесса = HomeCameraService (собранный exe)
```powershell
# собрать HomeCameraService.exe (без консоли) и положить к приложению
powershell -ExecutionPolicy Bypass -File build_service.ps1
# затем перерегистрировать задачу — она автоматически возьмёт exe
powershell -ExecutionPolicy Bypass -File install.ps1
```
Диспетчер задач покажет процесс **HomeCameraService.exe**. Приложение под
супервизором остаётся `pythonw.exe`.

## 4. Перезапуск при падении — два уровня
1. **Планировщик:** `RestartOnFailure` (интервал 1 мин, 999 попыток) — оживит,
   если упадёт сам супервизор/задача (ненулевой код возврата).
2. **Супервизор:** `supervisor.py` перезапускает приложение при любом завершении,
   с нарастающей паузой (10 с → до 60 с при частых падениях) и сбросом задержки,
   если процесс проработал ≥ 60 с.

## 5. Управление и проверка
```powershell
Start-ScheduledTask   -TaskName HomeCameraService
Stop-ScheduledTask    -TaskName HomeCameraService
Get-ScheduledTask     -TaskName HomeCameraService | Select TaskName,State
Get-ScheduledTaskInfo -TaskName HomeCameraService    # LastTaskResult, NextRunTime
```
```cmd
:: процессы идут, окон нет
tasklist /v /fi "imagename eq HomeCameraService.exe"
tasklist /v /fi "imagename eq pythonw.exe"
:: 0x0 = успех, 0x41301 = выполняется
schtasks /query /tn HomeCameraService /v /fo LIST | findstr /i "result status"
```
Логи супервизора: `<папка HomeCameraService>\service.log`.
Логи приложения: `logs\app.log`.

## 6. Вариант через schtasks (эквивалент install.ps1)
```cmd
:: XML должен быть в кодировке UTF-16LE с BOM; поправьте пути внутри.
schtasks /create /tn HomeCameraService /xml C:\HomeCamera\camera_service.xml /f
schtasks /run    /tn HomeCameraService
```
Удаление:
```cmd
schtasks /end    /tn HomeCameraService
schtasks /delete /tn HomeCameraService /f
```

## 7. Выбор учётной записи
- **`SYSTEM`** (по умолчанию): старт при загрузке, без входа пользователя.
  Нет доступа к сетевым дискам по букве и к рабочему столу. Учтите: **микрофон
  и камера под SYSTEM** доступны как устройства, но если аудио-устройство
  привязано к сессии пользователя — используйте вариант с учёткой пользователя.
- **Учётка пользователя** (`install.ps1 -User "DOMAIN\User"`, LogonType S4U):
  нужна, если камера/микрофон/сохранение завязаны на профиль или GUI-сессию.

## 8. Диагностика
- Не запускается — проверьте права на папку проекта и на устройство камеры/микрофона.
- Сразу падает — смотрите `service.log`; запустите вручную
  `python.exe main.py` из-под той же учётки, что и задача.
- Камера/аудио ещё не готовы при загрузке — увеличьте `Delay` в триггере.
- `pythonw.exe` не найден — укажите путь явно: `install.ps1 -Pythonw "C:\Python311\pythonw.exe"`.
