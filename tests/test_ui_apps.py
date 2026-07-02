"""Tests the generic app-detection + adapter layer (Qt-free, cross-platform)."""

from winspark.domain.enums import WindowStateKind
from winspark.domain.models import WindowInfo
from winspark.ui.apps import adapter_for_key, detect_running_apps


def _win(handle, process, title, active=False):
    return WindowInfo(handle=handle, title=title, process_name=process, is_active=active)


def test_groups_multiple_windows_of_one_process_into_one_app():
    apps = detect_running_apps([
        _win(1, "chrome.exe", "Gmail - Google Chrome"),
        _win(2, "chrome.exe", "YouTube - Google Chrome"),
    ])
    chrome = [a for a in apps if a.process_name == "chrome.exe"]
    assert len(chrome) == 1
    assert chrome[0].window_count == 2
    assert chrome[0].display_name == "Chrome"


def test_whatsapp_is_a_supported_app():
    apps = detect_running_apps([_win(1, "WhatsApp.Root.exe", "WhatsApp")])
    assert len(apps) == 1
    assert apps[0].display_name == "WhatsApp"
    assert apps[0].supported is True
    assert apps[0].adapter_key == "whatsapp"


def test_whatsapp_webview_host_merges_into_the_whatsapp_app():
    # The desktop shell process and its embedded webview (a separate process,
    # titled "(4) WhatsApp") should appear as one WhatsApp app, not two.
    apps = detect_running_apps([
        _win(1, "WhatsApp.Root.exe", "WhatsApp"),
        _win(2, "msedgewebview2.exe", "(4) WhatsApp"),
    ])
    whatsapp = [a for a in apps if a.adapter_key == "whatsapp"]
    assert len(whatsapp) == 1
    assert whatsapp[0].window_count == 2


def test_unsupported_app_is_observe_only():
    apps = detect_running_apps([_win(1, "notepad.exe", "Untitled - Notepad")])
    assert apps[0].supported is False
    assert apps[0].adapter_key is None
    assert apps[0].display_name == "Notepad"


def test_system_noise_is_hidden():
    apps = detect_running_apps([
        _win(1, "explorer.exe", "Program Manager"),
        _win(2, "TextInputHost.exe", "Windows Input Experience"),
        _win(3, "notepad.exe", "Untitled - Notepad"),
    ])
    names = [a.display_name for a in apps]
    assert names == ["Notepad"]


def test_supported_apps_are_listed_first_then_alphabetical():
    apps = detect_running_apps([
        _win(1, "notepad.exe", "Untitled - Notepad"),
        _win(2, "chrome.exe", "Google Chrome"),
        _win(3, "WhatsApp.Root.exe", "WhatsApp"),
    ])
    assert apps[0].display_name == "WhatsApp"          # supported first
    assert [a.display_name for a in apps[1:]] == ["Chrome", "Notepad"]  # then alphabetical


def test_active_flag_is_true_if_any_window_active():
    apps = detect_running_apps([
        _win(1, "chrome.exe", "A", active=False),
        _win(2, "chrome.exe", "B", active=True),
    ])
    assert apps[0].is_active is True


def test_adapter_for_key():
    assert adapter_for_key("whatsapp").display_name == "WhatsApp"
    assert adapter_for_key("nope") is None
    assert adapter_for_key(None) is None
