# Build winSpark.exe with PyInstaller, using the project's virtualenv.
# Run from winspark_py/:   ./build_exe.ps1
# Output: dist/winSpark.exe

$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv/Scripts/python.exe"

Write-Host "Ensuring PyInstaller is installed..." -ForegroundColor Cyan
& $python -m pip install --quiet --upgrade pyinstaller

Write-Host "Building winSpark.exe..." -ForegroundColor Cyan
& $python -m PyInstaller --noconfirm --clean (Join-Path $PSScriptRoot "winspark.spec")

Write-Host "Done. See dist/winSpark.exe" -ForegroundColor Green
