"""Save incoming STICKERS (webp, animated included).

Animated stickers are flagged in the sidecar JSON via `is_animated`, which the
screenshot approach can't tell you — a still capture of an animated sticker is
one arbitrary frame.

    python save_stickers.py
"""

import wa_runner

if __name__ == "__main__":
    wa_runner.run({"sticker"}, label="stickers")
