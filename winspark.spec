# PyInstaller build spec for winSpark — build with:  pyinstaller winspark.spec
#
# Produces a single windowed winSpark.exe (no console) with the app icon. The
# tricky dependencies are the ones loaded dynamically at runtime — the Windows
# OCR (winrt.*), UI Automation (uiautomation/comtypes), and pywin32 — which
# PyInstaller's static analysis can miss, so they're pulled in explicitly below.

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

hiddenimports = []
# winspark itself imports several submodules lazily (inside functions), so
# collect the whole package rather than relying on static following.
hiddenimports += collect_submodules("winspark")
# Windows Runtime OCR + imaging, imported by string at call time.
hiddenimports += collect_submodules("winrt")
hiddenimports += [
    "winrt.windows.media.ocr",
    "winrt.windows.graphics.imaging",
    "winrt.windows.storage.streams",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.globalization",
]
# UI Automation + its COM backend.
hiddenimports += collect_submodules("uiautomation")
hiddenimports += ["comtypes", "comtypes.client"]
# pywin32 modules used across discovery / capture / foregrounding.
hiddenimports += [
    "win32gui", "win32process", "win32ui", "win32con", "win32api",
    "pythoncom", "pywintypes",
]

datas = [
    ("winspark/ui/assets/winspark.ico", "winspark/ui/assets"),
    ("winspark/ui/assets/winspark.png", "winspark/ui/assets"),
]
binaries = []
for pkg in ("winrt",):
    try:
        datas += collect_data_files(pkg)
        binaries += collect_dynamic_libs(pkg)
    except Exception:
        pass


a = Analysis(
    ["run_gui.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="winSpark",
    debug=False,
    strip=False,
    upx=False,
    console=False,               # windowed app — no console window
    icon="winspark/ui/assets/winspark.ico",
)
