"""Pinned apps: stored in data.json, surviving restarts and app closure.

The point of a pin is the app being CLOSED — the entry must persist with
enough information (the executable path) to launch it again.
"""

import json

from winspark.ui.pins import PinStore


def test_a_pin_survives_a_restart(tmp_path):
    PinStore(tmp_path).pin("VS Code", "code.exe", r"C:\apps\code.exe")
    pins = PinStore(tmp_path).pins()

    assert len(pins) == 1
    assert pins[0].name == "VS Code"
    assert pins[0].process == "code.exe"
    assert pins[0].path == r"C:\apps\code.exe"


def test_pinning_twice_replaces_rather_than_duplicating(tmp_path):
    store = PinStore(tmp_path)
    store.pin("VS Code", "code.exe", r"C:\old\code.exe")
    store.pin("VS Code", "Code.EXE", r"C:\new\code.exe")   # case-insensitive identity

    assert len(store.pins()) == 1
    assert store.pins()[0].path == r"C:\new\code.exe"


def test_unpin_removes_and_persists(tmp_path):
    store = PinStore(tmp_path)
    store.pin("VS Code", "code.exe")
    store.unpin("CODE.exe")

    assert store.pins() == ()
    assert PinStore(tmp_path).pins() == ()


def test_is_pinned_matches_regardless_of_case(tmp_path):
    store = PinStore(tmp_path)
    store.pin("Slack", "slack.exe")
    assert store.is_pinned("Slack.exe") is True
    assert store.is_pinned("code.exe") is False


def test_an_unreadable_file_means_no_pins_not_a_crash(tmp_path):
    (tmp_path / "data.json").write_text("{not json", encoding="utf-8")
    assert PinStore(tmp_path).pins() == ()


def test_malformed_rows_are_dropped_good_ones_kept(tmp_path):
    (tmp_path / "data.json").write_text(json.dumps({
        "pinned_apps": [
            {"name": "Good", "process": "good.exe"},
            {"name": "", "process": "nameless.exe"},     # unusable
            "not an object",
        ],
    }), encoding="utf-8")

    assert [p.process for p in PinStore(tmp_path).pins()] == ["good.exe"]


def test_pins_do_not_wipe_other_keys_in_data_json(tmp_path):
    """data.json is shared — a pin change must merge, not overwrite the file."""
    (tmp_path / "data.json").write_text(json.dumps({"something_else": {"kept": True}}),
                                        encoding="utf-8")
    PinStore(tmp_path).pin("Slack", "slack.exe")

    on_disk = json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))
    assert on_disk["something_else"] == {"kept": True}
    assert on_disk["pinned_apps"][0]["process"] == "slack.exe"


def test_blank_names_are_refused(tmp_path):
    store = PinStore(tmp_path)
    store.pin("", "code.exe")
    store.pin("VS Code", "  ")
    assert store.pins() == ()
