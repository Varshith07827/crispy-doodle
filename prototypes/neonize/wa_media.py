"""Classify an incoming WhatsApp message's media and save it to disk.

This is the piece winSpark cannot do today. The accessibility tree it drives
today can only *name* an attachment ("[Photo]", "[Voice note · 0:12]") — the
bytes are never exposed, so `whatsapp.save_media_thumbnails` resorts to
screenshotting the window region, which cannot reach a voice note or a
document at all (see winspark/constants.py).

neonize speaks the multi-device protocol, so `client.download_any(message)`
returns the real, original-quality file.

Everything here is deliberately free of any live client: `classify()` and
`plan_save()` are pure functions over a protobuf, so the whole naming/metadata
path is testable offline (test_wa_media.py) without pairing an account.
"""

from __future__ import annotations

import json
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# WhatsApp's mimetypes carry codec parameters ("audio/ogg; codecs=opus") and
# mimetypes.guess_extension() gets several of them wrong or returns None, so
# the ones that actually turn up are pinned rather than guessed.
_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/3gpp": ".3gp",
    "audio/ogg": ".ogg",          # voice notes: opus in an ogg container
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "application/pdf": ".pdf",
}

# Which Message field holds which kind of attachment. Verified by introspecting
# the installed protobuf rather than taken from documentation.
_MEDIA_FIELDS = (
    ("imageMessage", "image"),
    ("videoMessage", "video"),
    ("audioMessage", "audio"),
    ("documentMessage", "document"),
    ("stickerMessage", "sticker"),
    ("ptvMessage", "video_note"),
)

# Wrappers that carry a real media message one level down.
_WRAPPERS = ("documentWithCaptionMessage", "viewOnceMessage", "viewOnceMessageV2",
             "viewOnceMessageV2Extension", "ephemeralMessage")

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Media:
    """One downloadable attachment found on a message."""

    kind: str                 # image | video | audio | voice | document | sticker | video_note
    submessage: Any           # the ImageMessage/AudioMessage/... protobuf
    mimetype: str = ""
    caption: str = ""
    file_name: str = ""       # documents carry their original name
    seconds: int = 0          # audio/video duration
    width: int = 0
    height: int = 0
    page_count: int = 0
    file_length: int = 0      # what WhatsApp says the size is, for verification
    is_animated: bool = False
    view_once: bool = False

    @property
    def extension(self) -> str:
        base = (self.mimetype or "").split(";")[0].strip().lower()
        if base in _EXTENSIONS:
            return _EXTENSIONS[base]
        if self.file_name and "." in self.file_name:
            return Path(self.file_name).suffix
        return mimetypes.guess_extension(base) or ".bin"


def _text(obj: Any, name: str, default: str = "") -> Any:
    return getattr(obj, name, default)


def unwrap(message: Any) -> Any:
    """Follow wrapper messages (view-once, ephemeral, document-with-caption)
    down to the one actually holding media. Without this, a view-once photo
    looks like a message with no media at all."""
    seen = 0
    while seen < 5:  # depth guard: a malformed chain must not spin
        for wrapper in _WRAPPERS:
            try:
                if message.HasField(wrapper):
                    inner = getattr(message, wrapper)
                    message = getattr(inner, "message", inner)
                    break
            except ValueError:
                continue          # field not present in this protobuf version
        else:
            return message
        seen += 1
    return message


def classify(message: Any) -> Optional[Media]:
    """The attachment on this message, or None if it's text-only.

    `audio` splits into `voice` when the PTT flag is set — a recorded voice
    note and a shared music file are the same protobuf but very different
    things to a user, and winSpark's chat memory already distinguishes them.
    """
    message = unwrap(message)
    for field_name, kind in _MEDIA_FIELDS:
        try:
            present = message.HasField(field_name)
        except ValueError:
            continue              # older/newer protobuf without this field
        if not present:
            continue
        sub = getattr(message, field_name)
        if kind == "audio" and bool(_text(sub, "PTT", False)):
            kind = "voice"
        return Media(
            kind=kind,
            submessage=sub,
            mimetype=_text(sub, "mimetype", ""),
            caption=_text(sub, "caption", ""),
            file_name=_text(sub, "fileName", ""),
            seconds=int(_text(sub, "seconds", 0) or 0),
            width=int(_text(sub, "width", 0) or 0),
            height=int(_text(sub, "height", 0) or 0),
            page_count=int(_text(sub, "pageCount", 0) or 0),
            file_length=int(_text(sub, "fileLength", 0) or 0),
            is_animated=bool(_text(sub, "isAnimated", False)),
            view_once=bool(_text(sub, "viewOnce", False)),
        )
    return None


def safe_slug(text: str, limit: int = 40) -> str:
    """A filesystem-safe fragment. Chat and sender names routinely contain
    emoji, '/' and ':' — all of which make an unusable Windows filename."""
    cleaned = _UNSAFE.sub("_", (text or "").strip()).strip("._-")
    return (cleaned[:limit] or "unknown")


@dataclass
class SavePlan:
    """Where a download will go, decided before any bytes move."""

    path: Path
    metadata_path: Path
    metadata: dict = field(default_factory=dict)


def plan_save(media: Media, out_dir: Path, *, chat: str = "", sender: str = "",
              message_id: str = "", when: Optional[datetime] = None) -> SavePlan:
    """Decide the filename and the sidecar metadata for an attachment.

    Named by timestamp + chat + kind + short message id: sortable, unique
    across senders, and stable if the same file is sent twice (the id differs,
    so nothing is silently overwritten)."""
    when = when or datetime.now(timezone.utc)
    stamp = when.strftime("%Y%m%d-%H%M%S")
    parts = [stamp, safe_slug(chat, 24), media.kind]
    if message_id:
        parts.append(safe_slug(message_id, 12))
    stem = "-".join(p for p in parts if p)
    directory = out_dir / media.kind
    metadata = {
        "kind": media.kind,
        "chat": chat,
        "sender": sender,
        "message_id": message_id,
        "received_utc": when.isoformat(),
        "mimetype": media.mimetype,
        "caption": media.caption,
        "original_file_name": media.file_name,
        "declared_bytes": media.file_length,
        "seconds": media.seconds,
        "width": media.width,
        "height": media.height,
        "page_count": media.page_count,
        "is_animated": media.is_animated,
        "view_once": media.view_once,
    }
    return SavePlan(
        path=directory / f"{stem}{media.extension}",
        metadata_path=directory / f"{stem}.json",
        metadata={k: v for k, v in metadata.items() if v not in ("", 0, False)},
    )


def write_media(plan: SavePlan, data: bytes) -> dict:
    """Write the bytes and the sidecar. Returns the metadata actually written,
    including the real size — compared against WhatsApp's declared length so a
    truncated download is visible rather than silently accepted."""
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    plan.path.write_bytes(data)
    metadata = dict(plan.metadata)
    metadata["saved_as"] = plan.path.name
    metadata["actual_bytes"] = len(data)
    declared = metadata.get("declared_bytes")
    if declared and declared != len(data):
        metadata["size_mismatch"] = True
    plan.metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
    return metadata


def save_from_event(client: Any, message: Any, out_dir: Path, *, chat: str = "",
                    sender: str = "", message_id: str = "") -> Optional[dict]:
    """Classify, download and save the attachment on `message`. Returns the
    metadata written, or None when the message carries no media."""
    media = classify(message)
    if media is None:
        return None
    plan = plan_save(media, out_dir, chat=chat, sender=sender, message_id=message_id)
    data = client.download_any(message)
    return write_media(plan, data)
