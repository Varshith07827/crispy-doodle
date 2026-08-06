"""Run the full save pipeline offline, producing real files on disk.

Everything is exercised except the one `client.download_any()` network call:
real WhatsApp protobufs go in, real bytes come out, and media/ ends up laid out
exactly as it will be once an account is paired. The image, sticker and PDF are
genuinely valid files you can open; the audio/video payloads are labelled
placeholders (generating real opus/h264 needs an encoder, and the byte path is
identical either way).

    python demo_save.py
"""

from __future__ import annotations

import io
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

import wa_media

OUT = Path(__file__).parent / "media"


def _real_image(fmt: str, size=(320, 200)) -> bytes:
    """A genuinely valid image file, via Pillow (already a neonize dependency)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, (14, 88, 74))
    draw = ImageDraw.Draw(img)
    draw.rectangle([12, 12, size[0] - 12, size[1] - 12], outline=(210, 245, 230), width=3)
    draw.text((30, 90), f"winSpark {fmt} demo", fill=(232, 252, 244))
    buffer = io.BytesIO()
    img.save(buffer, format=fmt)
    return buffer.getvalue()


def _real_pdf() -> bytes:
    """A minimal but genuinely valid one-page PDF."""
    body = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 120]"
        b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
        b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"5 0 obj<</Length 62>>stream\n"
        b"BT /F1 14 Tf 24 60 Td (winSpark media prototype) Tj ET\n"
        b"endstream endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )
    return body


def _message(kind: str) -> tuple[Message, bytes]:
    msg = Message()
    if kind == "image":
        msg.imageMessage.mimetype = "image/jpeg"
        msg.imageMessage.caption = "sunset from the balcony"
        msg.imageMessage.width, msg.imageMessage.height = 320, 200
        data = _real_image("JPEG")
        msg.imageMessage.fileLength = len(data)
        return msg, data
    if kind == "sticker":
        msg.stickerMessage.mimetype = "image/webp"
        data = _real_image("WEBP", (256, 256))
        msg.stickerMessage.fileLength = len(data)
        return msg, data
    if kind == "document":
        data = _real_pdf()
        msg.documentMessage.mimetype = "application/pdf"
        msg.documentMessage.fileName = "Q3 report.pdf"
        msg.documentMessage.pageCount = 1
        msg.documentMessage.fileLength = len(data)
        return msg, data
    if kind == "voice":
        msg.audioMessage.mimetype = "audio/ogg; codecs=opus"
        msg.audioMessage.PTT = True
        msg.audioMessage.seconds = 12
        data = b"OggS" + b"\x00" * 60 + b"[placeholder opus payload]"
        msg.audioMessage.fileLength = len(data)
        return msg, data
    if kind == "audio":
        msg.audioMessage.mimetype = "audio/mpeg"
        msg.audioMessage.seconds = 154
        data = b"ID3" + b"\x00" * 60 + b"[placeholder mp3 payload]"
        msg.audioMessage.fileLength = len(data)
        return msg, data
    if kind == "video":
        msg.videoMessage.mimetype = "video/mp4"
        msg.videoMessage.seconds = 30
        msg.videoMessage.caption = "the dog again"
        data = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 40 + b"[placeholder mp4 payload]"
        msg.videoMessage.fileLength = len(data)
        return msg, data
    raise ValueError(kind)


class _Client:
    """Stubs only the network call; every other line of the pipeline is real."""

    def __init__(self):
        self.payload = b""

    def download_any(self, _message):
        return self.payload


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)

    client = _Client()
    when = datetime(2026, 8, 3, 14, 30, 0, tzinfo=timezone.utc)
    kinds = ["image", "sticker", "document", "voice", "audio", "video"]

    print(f"writing into {OUT}\n")
    for i, kind in enumerate(kinds):
        msg, client.payload = _message(kind)
        # plan_save is given the timestamp so the demo output is reproducible;
        # the live path uses "now".
        media = wa_media.classify(msg)
        plan = wa_media.plan_save(media, OUT, chat="Nagen US", sender="919876543210",
                                  message_id=f"DEMO{i:04d}ABCD",
                                  when=when + timedelta(seconds=i * 7))
        meta = wa_media.write_media(plan, client.download_any(msg))
        detail = []
        if meta.get("seconds"):
            detail.append(f"{meta['seconds']}s")
        if meta.get("width"):
            detail.append(f"{meta['width']}x{meta['height']}")
        if meta.get("caption"):
            detail.append(f'"{meta["caption"]}"')
        print(f"  {meta['kind']:<9} {meta['actual_bytes']:>7,} bytes  "
              f"{meta['saved_as']}{('  ' + ', '.join(detail)) if detail else ''}")

    print("\nresulting tree:")
    for path in sorted(OUT.rglob("*")):
        if path.is_file():
            print(f"  media/{path.relative_to(OUT).as_posix():<62} {path.stat().st_size:>7,}")

    sample = next((OUT / "image").glob("*.json"))
    print(f"\nsidecar example ({sample.name}):")
    print("".join(f"  {line}\n" for line in sample.read_text(encoding="utf-8").splitlines()))


if __name__ == "__main__":
    main()
