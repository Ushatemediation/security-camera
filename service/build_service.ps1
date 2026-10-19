param(
    [string]$ProjectDir = (Join-Path $PSScriptRoot ".."),
    [string]$Name       = "HomeCameraService"
)
# build_service.ps1 — собрать HomeCameraService.exe из supervisor.py (PyInstaller).
# exe не открывает консоль (--noconsole) и виден в Диспетчере под именем
# HomeCameraService.exe. После сборки install.ps1 подхватит его автоматически.
#
# ЗАПУСКАТЬ В VER-ОКРУЖЕНИИ ПРОЕКТА (там, где установлен pyinstaller):
#   powershell -ExecutionPolicy Bypass -File build_service.ps1

$ErrorActionPreference = "Stop"

$Supervisor = Join-Path $PSScriptRoot "supervisor.py"
if (-not (Test-Path $Supervisor)) { throw "Не найден supervisor.py рядом со скриптом." }

$ProjectDir = (Resolve-Path $ProjectDir).Path

# 1. pyinstaller
$has = $false
try { python -m PyInstaller --version *> $null; $has = ($LASTEXITCODE -eq 0) } catch {}
if (-not $has) {
    Write-Host "Устанавливаю pyinstaller..."
    python -m pip install --upgrade pyinstaller
}

# 2. Сборка одного файла без консоли
$dist  = Join-Path $PSScriptRoot "dist"
$build = Join-Path $PSScriptRoot "build"
python -m PyInstaller --noconfirm --clean --onefile --noconsole `
    --name     $Name `
    --distpath $dist `
    --workpath $build `
    --specpath $build `
    $Supervisor

# 3. Копируем exe к приложению (супервизор сам найдёт ../main.py,
#    либо примет путь аргументом/через HOME_CAMERA_APP)
$built = Join-Path $dist "$Name.exe"
$dest  = Join-Path $ProjectDir "$Name.exe"
Copy-Item $built $dest -Force
Write-Host "Готово: $dest"
Write-Host "Теперь запустите install.ps1 — задача будет использовать этот exe."
