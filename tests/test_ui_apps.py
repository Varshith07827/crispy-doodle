"""Tests the generic app-detection + adapter layer (Qt-free, cross-platform)."""

from winspark.domain.enums import WindowStateKind
from winspark.domain.models import WindowInfo
from winspark.ui.apps import adapter_for_key, detect_running_apps


def _win(handle, process, title, active=False, app_id=""):
    return WindowInfo(handle=handle, title=title, process_name=process, is_active=active,
                      app_user_model_id=app_id)


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


def test_alt_tab_rule_matches_windows_own_taskbar_eligibility():
    from winspark.engines.window_discovery import _passes_alt_tab_rule

    TOOL, APP = 0x00000080, 0x00040000
    # Measured live: real apps are plain top-levels; background utilities are
    # tool windows and/or owned. WS_EX_APPWINDOW opts back in.
    assert _passes_alt_tab_rule(0, has_owner=False) is True          # Chrome, WhatsApp...
    assert _passes_alt_tab_rule(TOOL, has_owner=False) is False       # PowerToys helper
    assert _passes_alt_tab_rule(TOOL, has_owner=True) is False        # OEM helper (Senary...)
    assert _passes_alt_tab_rule(0, has_owner=True) is False           # owned dialog shell
    assert _passes_alt_tab_rule(TOOL | APP, has_owner=True) is True   # explicit opt-in wins


def test_humanize_splits_camel_case_names():
    from winspark.ui.apps import _humanize

    assert _humanize("chrome.exe") == "Chrome"
    assert _humanize("SenaryAdvancedFeaturesApp.exe") == "Senary Advanced Features App"
    assert _humanize("WINWORD.EXE") == "Winword"
    assert _humanize("some_tool.name.exe") == "Some Tool Name"


def test_installed_web_app_is_its_own_app_not_a_browser_window():
    # Measured live: a plain Chrome window's AppUserModelID is "Chrome"; the
    # YouTube app's is "Chrome._crx_<id>" — Task View's own separation signal.
    apps = detect_running_apps([
        _win(1, "chrome.exe", "API Keys - GroqCloud - Google Chrome", app_id="Chrome"),
        _win(2, "chrome.exe", "YouTube", app_id="Chrome._crx_agimnkijcamfeangaknmldooml"),
    ])
    names = {a.display_name for a in apps}
    assert names == {"Chrome", "YouTube"}
    keys = {a.app_key for a in apps}
    assert len(keys) == 2  # distinct identities despite the shared process


def test_same_process_apps_get_distinct_keys():
    apps = detect_running_apps([
        _win(1, "ApplicationFrameHost.exe", "Microsoft Store"),
        _win(2, "ApplicationFrameHost.exe", "Settings"),
    ])
    assert len({a.app_key for a in apps}) == 2


def test_uwp_apps_are_named_by_their_window_title_not_the_host_process():
    # Store, Settings, etc. all run under ApplicationFrameHost — so the title
    # is the real app name, and two different UWP apps must be two entries,
    # not one merged "ApplicationFrameHost".
    apps = detect_running_apps([
        _win(1, "ApplicationFrameHost.exe", "Microsoft Store"),
        _win(2, "ApplicationFrameHost.exe", "Settings"),
        _win(3, "notepad.exe", "Untitled - Notepad"),
    ])
    names = {a.display_name for a in apps}
    assert names == {"Microsoft Store", "Settings", "Notepad"}


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


def test_known_apps_get_curated_friendly_names():
    # Apps whose executable humanizes poorly still read cleanly.
    apps = detect_running_apps([
        _win(1, "ms-teams.exe", "Chat | Microsoft Teams"),
        _win(2, "olk.exe", "Inbox - Outlook"),
        _win(3, "telegram.exe", "Telegram"),
    ])
    names = {a.process_name: a.display_name for a in apps}
    assert names["ms-teams.exe"] == "Microsoft Teams"
    assert names["olk.exe"] == "Outlook (new)"
    assert names["telegram.exe"] == "Telegram"


def test_unknown_process_still_humanizes():
    apps = detect_running_apps([_win(1, "SomeRandomApp.exe", "x")])
    assert apps[0].display_name == "Some Random App"
