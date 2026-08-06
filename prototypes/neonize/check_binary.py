"""Does the Go/whatsmeow native library actually load here? No network, no
pairing — just proves the ctypes layer is functional on this machine before
anyone scans a QR code.
"""

import sys
import time
from pathlib import Path

started = time.monotonic()
import neonize
from neonize.client import NewClient          # this is what forces the DLL open
from neonize.aioze.client import NewAClient
elapsed = time.monotonic() - started

pkg = Path(neonize.__file__).parent
binaries = [p for p in pkg.iterdir() if p.suffix in (".dll", ".so", ".dylib")]

print(f"python            : {sys.version.split()[0]} ({sys.platform})")
print(f"neonize           : {neonize.__file__}")
print(f"import took       : {elapsed:.2f}s")
for b in binaries:
    print(f"native library    : {b.name}  ({b.stat().st_size:,} bytes)")
print(f"bundled in wheel  : {'yes' if binaries else 'NO — fetched at runtime'}")
print(f"download_any      : sync={hasattr(NewClient, 'download_any')} "
      f"async={hasattr(NewAClient, 'download_any')}")
print("\nnative layer loaded OK — safe to pair")
