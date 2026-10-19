@echo off
chcp 65001 >nul
rem ============================================================================
rem  download_models.bat — заранее скачать веса моделей (интернет нужен один раз).
rem  yolov8n.pt  -> рядом с exe
rem  buffalo_l   -> %USERPROFILE%\.insightface\models\buffalo_l
rem ============================================================================
setlocal
cd /d "%~dp0"

where curl >nul 2>nul
if errorlevel 1 (
    echo [!] curl не найден ^(встроен в Windows 10/11; на старых — установите^).
    echo     Альтернатива: запустите камеру один раз с интернетом — модели
    echo     скачаются автоза.
    pause & exit /b 1
)

echo [*] yolov8n.pt...
if not exist "yolov8n.pt" (
    curl -L -o "yolov8n.pt" "https://github.com/ultralytics/assets/releases/download/v8.2.0/yolov8n.pt"
)

echo [*] InsightFace buffalo_l (~300 MB)...
set "DEST=%USERPROFILE%\.insightface\models"
if not exist "%DEST%" mkdir "%DEST%"
if not exist "%DEST%\buffalo_l\det_10g.onnx" (
    curl -L -o "%TEMP%\buffalo_l.zip" "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
    powershell -NoProfile -Command "Expand-Archive -Force '%TEMP%\buffalo_l.zip' '%DEST%\buffalo_l'"
    del /q "%TEMP%\buffalo_l.zip" >nul 2>&1
)

echo.
echo   ✓ Модели готовы. Можно запускать без интернета.
pause
endlocal
