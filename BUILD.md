# Building winSpark as a Windows app (.exe)

winSpark packages into a single windowed `winSpark.exe` with PyInstaller.

## Quick build

From `winspark_py/`:

```powershell
./build_exe.ps1
```

That installs PyInstaller into the project's `.venv` if needed, runs the build,
and writes **`dist/winSpark.exe`**. Double-click it — no Python install required
on the target machine.

## Manual build

```powershell
.venv\Scripts\python.exe -m pip install pyinstaller
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean winspark.spec
```

## The app icon

The icon lives at `winspark/ui/assets/winspark.ico` (with a `.png` alongside for
in-window use). Replace **`winspark.ico`** with your own — a multi-size `.ico`
(16–256 px) is ideal — and rebuild. No code changes needed; `winspark.spec`
already bundles whatever is at that path, and the running app loads it for the
window, the taskbar, and the `.exe` file icon.

To turn a single PNG into a proper multi-size `.ico`:

```powershell
.venv\Scripts\python.exe -c "from PIL import Image; Image.open('your.png').save('winspark/ui/assets/winspark.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
```

## Why it now shows "winSpark", not "Python"

A script run by `python.exe` inherits Python's taskbar identity. winSpark now
declares its own **AppUserModelID** (in `winspark/ui/branding.py`) before any
window opens, so Windows treats it as a distinct app with its own taskbar
button, grouping, and icon. This applies whether you run `python -m winspark.ui`
or the built `.exe`.

## Notes / troubleshooting

- **First launch is slower.** A one-file build unpacks to a temp folder on
  startup. If that bothers you, switch to a folder build: in `winspark.spec`
  replace the single `EXE(...)` with an `EXE(...)` + `COLLECT(...)` pair (the
  PyInstaller default onedir layout) — it starts faster but ships a folder
  instead of one file.
- **Runtime-loaded dependencies** — Windows OCR (`winrt.*`), UI Automation
  (`uiautomation`/`comtypes`), and `pywin32` — are declared as hidden imports in
  the spec. If a packaged build reports a missing module that the source run
  doesn't, add it to `hiddenimports` in `winspark.spec` and rebuild.
- **SmartScreen** may warn on an unsigned `.exe` the first time ("More info →
  Run anyway"). Code-signing removes that, but isn't required to run.
- The app writes its database to the same per-user location as the source run,
  so settings/automations carry over between running from source and the `.exe`.
