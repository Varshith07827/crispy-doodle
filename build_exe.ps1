# Build winSpark.exe with PyInstaller, using the project's virtualenv.
# Run from winspark_py/:   ./build_exe.ps1
# Output: dist/winSpark.exe
#
# The PyInstaller work directory lives OUTSIDE the project on purpose: this
# project sits inside OneDrive, whose sync engine locks the thousands of small
# files PyInstaller churns through (seen live as "Access is denied" deleting
# build\...\localpycs during --clean). Scratch goes to LOCALAPPDATA, which is
# never synced; only the final single exe lands in the project's dist/.
#
# ASCII only in this file: Windows PowerShell 5.1 reads BOM-less UTF-8 as
# ANSI, and a mangled em-dash inside a string becomes a smart quote that
# PowerShell treats as a string delimiter (seen live as "Missing closing '}'").

$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv/Scripts/python.exe"
$workpath = Join-Path $env:LOCALAPPDATA "winSpark\build"
$distpath = Join-Path $PSScriptRoot "dist"

Write-Host "Ensuring PyInstaller is installed..." -ForegroundColor Cyan
& $python -m pip install --quiet --upgrade pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }

# Clear leftover scratch from any previous (possibly OneDrive-locked) run.
foreach ($stale in @($workpath, (Join-Path $PSScriptRoot "build"))) {
    if (Test-Path $stale) {
        try {
            Remove-Item -Recurse -Force $stale -ErrorAction Stop
        } catch {
            Write-Host "Note: could not fully remove $stale - continuing." -ForegroundColor Yellow
        }
    }
}

Write-Host "Building winSpark.exe..." -ForegroundColor Cyan
& $python -m PyInstaller --noconfirm --workpath $workpath --distpath $distpath (Join-Path $PSScriptRoot "winspark.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }

$exe = Join-Path $distpath "winSpark.exe"
if (-not (Test-Path $exe)) { throw "Build reported success but $exe was not produced." }
Write-Host "Done. $exe" -ForegroundColor Green
