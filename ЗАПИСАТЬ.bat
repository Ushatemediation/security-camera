@echo off
chcp 65001 >nul
rem ============================================================================
rem  ЗАПИСАТЬ.bat — записать клип ПРЯМО СЕЙЧАС, как будто сработала детекция.
rem  Создаёт файл trigger.now рядом с программой; работающее приложение его
rem  увидит, запишет ~10 секунд, вшьёт звук, выгрузит на сервер -> в Telegram.
rem ============================================================================
setlocal
cd /d "%~dp0"

rem приложение должно быть запущено
set "EXE="
for %%f in (*.exe) do (
    if /i not "%%~nxf"=="ffmpeg.exe" if not defined EXE set "EXE=%%~ff"
)
if not defined EXE (
    echo [!] Не найден exe рядом со скриптом.
    pause & exit /b 1
)

tasklist /fi "imagename eq %~nx0" >nul 2>&1
for %%f in ("%EXE%") do set "EXENAME=%%~nxf"
tasklist /fi "imagename eq !EXENAME!" 2>nul | findstr /i "!EXENAME!" >nul
if errorlevel 1 (
    echo [!] Программа не запущена. Сначала START_CAMERA.bat, потом этот файл.
    pause & exit /b 1
)

echo. > "trigger.now"
echo.
echo   ✓ Команда отправлена.
echo   Через ~10 секунд клип уйдёт на сервер и придёт в Telegram.
echo   Файл появится в recordings\
timeout /t 4 >nul
endlocal
