"""Generic detection of running desktop apps + the app-adapter registry.

This is the application-independent core of the UI: it turns the raw discovered
windows into a deduplicated list of "running apps" a person would recognize, and
knows which apps winSpark can automate (via a registered adapter) vs. which it
can only observe. WhatsApp is simply the first adapter — adding Telegram,
Outlook, etc. later is a matter of registering another `AppAdapterInfo`, with no
change to this detection logic or the UI shell.

Deliberately Qt-free so it stays unit-testable and app-agnostic; the actual
per-app control panels live in the UI layer, keyed by `adapter_key`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from winspark.domain.models import WindowInfo

# Windows that aren't user-facing applications — hidden from the app list so a
# non-technical user only sees real apps. Kept intentionally small.
_NOISE_TITLES = {"program manager", "windows input experience"}
_NOISE_PROCESSES = {"textinputhost.exe", "applicationframehost.exe"}

# Processes that host an embedded web app (so a window titled "WhatsApp" running
# under one of these belongs to the WhatsApp desktop app, not a generic browser).
_WEBVIEW_HOST_PROCESSES = {"msedgewebview2.exe"}


@dataclass(frozen=True, slots=True)
class AppAdapterInfo:
    """Metadata describing an app winSpark can automate. The matching rules stay
    here; the panel that drives it lives in the UI layer keyed by `key`."""

    key: str
    display_name: str
    process_names: frozenset[str]          # lowercase exact process matches
    title_keyword: Optional[str] = None    # also match a webview host window with this in its title

    def matches(self, window: WindowInfo) -> bool:
        process = window.process_name.lower()
        if process in self.process_names:
            return True
        if (
            self.title_keyword
            and self.title_keyword in window.title.lower()
            and process in _WEBVIEW_HOST_PROCESSES
        ):
            return True
        return False


# The adapter registry. Add new supported apps here.
ADAPTERS: tuple[AppAdapterInfo, ...] = (
    AppAdapterInfo(
        key="whatsapp",
        display_name="WhatsApp",
        process_names=frozenset({"whatsapp.exe", "whatsapp.root.exe"}),
        title_keyword="whatsapp",
    ),
)


@dataclass(frozen=True, slots=True)
class RunningApp:
    """One app the user would recognize, aggregated from its windows."""

    display_name: str
    process_name: str
    adapter_key: Optional[str]        # set if winSpark can automate this app
    window_handles: tuple[int, ...]
    primary_title: str
    is_active: bool

    @property
    def supported(self) -> bool:
        return self.adapter_key is not None

    @property
    def window_count(self) -> int:
        return len(self.window_handles)


def adapter_for_key(key: Optional[str]) -> Optional[AppAdapterInfo]:
    if key is None:
        return None
    return next((a for a in ADAPTERS if a.key == key), None)


def _matching_adapter(window: WindowInfo) -> Optional[AppAdapterInfo]:
    return next((a for a in ADAPTERS if a.matches(window)), None)


def _is_noise(window: WindowInfo) -> bool:
    return (
        not window.title.strip()
        or window.title.strip().lower() in _NOISE_TITLES
        or window.process_name.lower() in _NOISE_PROCESSES
    )


def _humanize(process_name: str) -> str:
    name = process_name[:-4] if process_name.lower().endswith(".exe") else process_name
    name = name.replace(".", " ").replace("_", " ")
    return name.strip().title() or process_name


def detect_running_apps(windows: list[WindowInfo]) -> list[RunningApp]:
    """Group discovered windows into recognizable apps. Windows a supported
    adapter claims are merged under that app (even across processes, e.g. the
    WhatsApp desktop shell plus its embedded webview); the rest group by
    process. Sorted supported-first, then by name."""
    order: list[str] = []
    groups: dict[str, dict] = {}

    for window in windows:
        if _is_noise(window):
            continue

        adapter = _matching_adapter(window)
        if adapter is not None:
            key = f"app:{adapter.key}"
            display_name = adapter.display_name
            adapter_key = adapter.key
        else:
            key = f"proc:{window.process_name.lower()}"
            display_name = _humanize(window.process_name)
            adapter_key = None

        group = groups.get(key)
        if group is None:
            order.append(key)
            group = {
                "display_name": display_name,
                "process_name": window.process_name,
                "adapter_key": adapter_key,
                "handles": [],
                "primary_title": window.title,
                "is_active": False,
            }
            groups[key] = group

        group["handles"].append(window.handle)
        group["is_active"] = group["is_active"] or window.is_active
        if window.is_active or not group["primary_title"]:
            group["primary_title"] = window.title

    apps = [
        RunningApp(
            display_name=g["display_name"],
            process_name=g["process_name"],
            adapter_key=g["adapter_key"],
            window_handles=tuple(g["handles"]),
            primary_title=g["primary_title"],
            is_active=g["is_active"],
        )
        for g in (groups[k] for k in order)
    ]
    apps.sort(key=lambda a: (not a.supported, a.display_name.lower()))
    return apps
