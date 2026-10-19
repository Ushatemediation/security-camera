@echo off
chcp 65001 >nul
rem ============================================================================
rem  PACK_RELEASE.bat — собрать релизный ZIP для раздачи:
rem  HomeCamera_v1.0_Windows.zip  (exe + модели + ffmpeg + config + батники).
rem  Сначала собирает проект (build.bat), затем упаковывает.
rem ============================================================================
setlocal
cd /d "%~dp0"

set "VER=1.0"
set "NAME=HomeCamera_v%VER%_Windows"
set "OUT=dist\HomeCamera"
set "STAGE=dist\%NAME%"

rem 1) сборка при необходимости
if not exist "%OUT%\HomeCamera.exe" (
    echo [*] Сборки нет — запускаю build.bat...
    call build.bat
)

if not exist "%OUT%\HomeCamera.exe" (
    echo [!] Сборка не найдена: %OUT%\HomeCamera.exe
    pause & exit /b 1
)

rem 2) докладываю батники и документацию
echo [*] Кладу в сборку батники и README...
for %%F in (START_CAMERA.bat INSTALL_AUTOSTART.bat UNINSTALL.bat download_models.bat) do (
    if exist "%%F" copy /y "%%F" "%OUT%\" >nul
)
if exist README.md copy /y README.md "%OUT%\README.md" >nul
if not exist "%OUT%\config.yaml" copy /y config.example.yaml "%OUT%\config.yaml" >nul

rem 3) переименовываю папку в релизное имя и упаковываю
echo [*] Упаковываю в ZIP...
if exist "%STAGE%" rmdir /s /q "%STAGE%"
move "%OUT%" "%STAGE%" >nul
if exist "dist\%NAME%.zip" del /q "dist\%NAME%.zip"
powershell -NoProfile -Command "Compress-Archive -Path '%STAGE%' -DestinationPath 'dist\%NAME%.zip' -Force"

echo.
echo   ✓ Готово: dist\%NAME%.zip
echo   Пользователь: скачал - распаковал - вписал токен в config.yaml - запустил START_CAMERA.bat
pause
endlocal
