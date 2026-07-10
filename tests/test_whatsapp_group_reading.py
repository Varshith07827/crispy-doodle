"""Tests for reading messages in GROUP chats — the fault that broke a live
demo: in a group, only OUR bubbles carry the "You:" label group, so the
labeled read saw one side of the conversation and "reply to the newest
incoming message" concluded there was never anything to reply to.

The Windows-UIA tree walking is stubbed; these test the selection logic in
_read_recent_messages_sync (which read path wins) — the part that decided the
demo's fate."""

import pytest

from winspark.connectors import whatsapp
from winspark.connectors.whatsapp import WhatsAppMessage


def _msg(text, incoming, sender=None):
    return WhatsAppMessage(
        sender=sender if sender is not None else ("" if incoming else "You"),
        text=text,
        is_incoming=incoming,
    )


@pytest.fixture
def stub_tree(monkeypatch):
    """Make _read_recent_messages_sync run against injected path results."""
    monkeypatch.setattr(whatsapp, "_require_win32", lambda: None)

    class _FakeAuto:
        @staticmethod
        def ControlFromHandle(_handle):
            return object()  # non-None sentinel; paths below are stubbed

    monkeypatch.setattr(whatsapp, "auto", _FakeAuto)

    def set_paths(labeled, bubbles):
        monkeypatch.setattr(whatsapp, "_read_labeled_messages", lambda root: list(labeled))
        monkeypatch.setattr(whatsapp, "_read_bubble_messages", lambda root: list(bubbles))

    return set_paths


def test_group_shape_uses_bubbles_when_labels_only_show_our_side(stub_tree):
    # The live-observed group state: labels see ONLY our messages, bubbles see
    # the whole conversation including members' messages.
    stub_tree(
        labeled=[_msg("I'll join later", incoming=False)],
        bubbles=[
            _msg("join me in our meet", incoming=True),
            _msg("I'll join later", incoming=False),
            _msg("Rara babu", incoming=True),
        ],
    )
    messages = whatsapp._read_recent_messages_sync(1, limit=10)
    assert len(messages) == 3
    assert messages[-1].text == "Rara babu" and messages[-1].is_incoming is True


def test_one_to_one_with_incoming_keeps_the_labeled_read(stub_tree):
    # Healthy 1:1: labels see both sides — must stay authoritative (senders!).
    stub_tree(
        labeled=[_msg("dinner at 8?", True, sender="Family"), _msg("sounds good", False)],
        bubbles=[_msg("dinner at 8?", True), _msg("sounds good", False), _msg("junk row", True)],
    )
    messages = whatsapp._read_recent_messages_sync(1, limit=10)
    assert [m.text for m in messages] == ["dinner at 8?", "sounds good"]
    assert messages[0].sender == "Family"


def test_all_outgoing_one_to_one_without_richer_bubbles_stays_labeled(stub_tree):
    # We messaged someone who never replied: all-outgoing is legitimate, and
    # the bubble read has nothing more — don't switch for no reason.
    stub_tree(
        labeled=[_msg("hello?", False)],
        bubbles=[_msg("hello?", False)],
    )
    messages = whatsapp._read_recent_messages_sync(1, limit=10)
    assert len(messages) == 1 and messages[0].is_incoming is False


def test_empty_labels_fall_back_to_bubbles(stub_tree):
    stub_tree(labeled=[], bubbles=[_msg("hi", True)])
    messages = whatsapp._read_recent_messages_sync(1, limit=10)
    assert len(messages) == 1 and messages[0].is_incoming is True


def test_limit_returns_the_newest_tail(stub_tree):
    stub_tree(labeled=[], bubbles=[_msg(f"m{i}", True) for i in range(10)])
    messages = whatsapp._read_recent_messages_sync(1, limit=3)
    assert [m.text for m in messages] == ["m7", "m8", "m9"]


# --- sender names in groups (fake UIA control tree) ---------------------------

class FakeControl:
    """Mimics the uiautomation control surface the readers touch: Name,
    ControlTypeName, GetChildren(), BoundingRectangle."""

    class _Rect:
        def __init__(self, left, right, top):
            self.left, self.right, self.top = left, right, top

    def __init__(self, control_type, name="", children=(), left=0, right=0, top=0):
        self.ControlTypeName = control_type
        self.Name = name
        self._children = list(children)
        self._parent = None
        for child in self._children:
            child._parent = self
        self.BoundingRectangle = FakeControl._Rect(left, right, top)

    def GetChildren(self):
        return list(self._children)

    def GetParentControl(self):
        return self._parent


def _incoming_row(text, top, sender_button=None, extra_buttons=()):
    children = []
    if sender_button:
        children.append(FakeControl("ButtonControl", sender_button))
    for b in extra_buttons:
        children.append(FakeControl("ButtonControl", b))
    children.append(FakeControl("TextControl", text, left=700, right=900, top=top))
    children.append(FakeControl("TextControl", "9:23 pm", left=910, right=960, top=top))
    return FakeControl("DataItemControl", children=children, top=top)


def _our_row(text, top):
    label = FakeControl("GroupControl", "You:", children=[
        FakeControl("TextControl", text, left=1500, right=1700, top=top),
    ])
    return FakeControl("DataItemControl", children=[label], top=top)


def _emoji_row(emoji, top, sender_button=None):
    children = []
    if sender_button:
        children.append(FakeControl("ButtonControl", sender_button))
    children.append(FakeControl("ImageControl", emoji, left=700, right=730, top=top))
    children.append(FakeControl("TextControl", "9:27 pm", left=910, right=960, top=top))
    return FakeControl("DataItemControl", children=children, top=top)


def _group_root(*rows):
    # Window 0..2000 wide; incoming text sits left, ours right of the 60% line.
    return FakeControl("PaneControl", children=list(rows), left=0, right=2000, top=0)


def test_group_sender_names_and_carry_forward():
    root = _group_root(
        _incoming_row("It's ok", top=100, sender_button="Manohar", extra_buttons=("Noah",)),
        _incoming_row("meet link", top=200, sender_button="Kushal Pavan Asuri"),
        _incoming_row("our meet", top=300),                      # follow-up: inherits Kushal
        _our_row("I'll join later", top=400),                    # ours — ends the run
        _incoming_row("Rara babu", top=500, sender_button="Manohar"),
    )
    messages = whatsapp._read_bubble_messages(root)
    assert [(m.sender, m.text, m.is_incoming) for m in messages] == [
        ("Manohar", "It's ok", True),          # first button wins over the pair
        ("Kushal Pavan Asuri", "meet link", True),
        ("Kushal Pavan Asuri", "our meet", True),   # carried forward
        ("You", "I'll join later", False),          # "You:" label is definitive
        ("Manohar", "Rara babu", True),
    ]


def test_status_buttons_are_not_senders():
    root = _group_root(
        _incoming_row("hello", top=100, sender_button=None,
                      extra_buttons=("9:21 pm Delivered ", "Forward media")),
    )
    messages = whatsapp._read_bubble_messages(root)
    assert messages[0].sender == ""  # no name invented from status buttons


def test_emoji_only_message_is_read_not_dropped():
    root = _group_root(
        _emoji_row("🧊", top=100, sender_button="Kushal Pavan Asuri"),
    )
    messages = whatsapp._read_bubble_messages(root)
    assert len(messages) == 1
    assert messages[0].text == "🧊"
    assert messages[0].sender == "Kushal Pavan Asuri"


def test_whatsapp_icon_glyphs_are_not_message_text():
    root = _group_root(
        FakeControl("DataItemControl", children=[
            FakeControl("ImageControl", "wds-ic-read", left=700, right=730, top=100),
        ], top=100),
    )
    assert whatsapp._read_bubble_messages(root) == []


def test_view_once_system_notice_is_dropped():
    # The "view once" placeholder renders as a bubble but carries nothing
    # readable — it was being misattributed as OUR message. Drop the row.
    row = FakeControl("DataItemControl", children=[
        FakeControl("ButtonControl", "Manohar"),
        FakeControl("TextControl",
                    "You received a view once message. For added privacy, you can only open it on your phone.",
                    left=700, right=1600, top=100),
        FakeControl("ButtonControl", "Learn more"),
        FakeControl("TextControl", "9:35 pm", left=1610, right=1660, top=100),
    ], top=100)
    assert whatsapp._read_bubble_messages(_group_root(row)) == []


def test_quoted_reply_preview_and_edited_marker_are_not_message_text():
    # A reply renders the quoted original under a "Quoted message" button —
    # seen live: its sender + preview ("Manohar Message") plus the "Edited"
    # marker leaked in as the reply's text.
    quote = FakeControl("ButtonControl", "Quoted message", children=[
        FakeControl("TextControl", "Manohar", left=700, right=760, top=100),
        FakeControl("ButtonControl", "Message", children=[
            FakeControl("TextControl", "Message", left=700, right=760, top=110),
        ]),
    ])
    row = FakeControl("DataItemControl", children=[
        FakeControl("ButtonControl", "Kushal Pavan Asuri"),
        quote,
        FakeControl("TextControl", "sure, changed it", left=700, right=850, top=120),
        FakeControl("TextControl", "Edited", left=860, right=900, top=120),
        FakeControl("TextControl", "9:36 pm", left=910, right=960, top=120),
    ], top=100)
    messages = whatsapp._read_bubble_messages(_group_root(row))
    assert len(messages) == 1
    assert messages[0].text == "sure, changed it"     # quote + marker excluded
    assert messages[0].sender == "Kushal Pavan Asuri"


def test_reply_with_no_remaining_text_is_dropped_not_junk():
    # Kushal's live row: quote + "Edited" and no message text of its own —
    # previously read as the junk message "Manohar Message Edited".
    quote = FakeControl("ButtonControl", "Quoted message", children=[
        FakeControl("TextControl", "Manohar", left=700, right=760, top=100),
    ])
    row = FakeControl("DataItemControl", children=[
        FakeControl("ButtonControl", "Kushal Pavan Asuri"),
        quote,
        FakeControl("TextControl", "Edited", left=860, right=900, top=100),
        FakeControl("TextControl", "9:36 pm", left=910, right=960, top=100),
    ], top=100)
    assert whatsapp._read_bubble_messages(_group_root(row)) == []


def test_emoji_inside_a_text_message_is_kept_in_order():
    # WhatsApp renders mid-text emoji as ImageControls BETWEEN the text runs
    # (seen live: Text "works! " + Image "🚀"); reading only text controls
    # silently dropped them.
    row = FakeControl("DataItemControl", children=[
        FakeControl("ButtonControl", "Kushal Pavan Asuri"),
        FakeControl("TextControl", "party tonight", left=700, right=830, top=100),
        FakeControl("ImageControl", "🎉", left=835, right=855, top=100),
        FakeControl("TextControl", "at my place", left=860, right=960, top=100),
        FakeControl("TextControl", "9:36 pm", left=970, right=1020, top=100),
    ], top=100)
    messages = whatsapp._read_bubble_messages(_group_root(row))
    assert messages[0].text == "party tonight 🎉 at my place"


def test_labeled_message_keeps_inline_emoji_too():
    # Same fix on the 1:1 (labeled) read path: message parts are SIBLINGS of
    # the sender-label group inside the row; the label subtree itself is skipped.
    label = FakeControl("GroupControl", "You:")
    row = FakeControl("DataItemControl", children=[
        label,
        FakeControl("TextControl", "send path works!", left=1500, right=1650, top=50),
        FakeControl("ImageControl", "🚀", left=1655, right=1675, top=50),
    ], top=50)
    _root = _group_root(row)  # gives the label a parent chain
    assert whatsapp._extract_bubble_text(label) == "send path works! 🚀"
