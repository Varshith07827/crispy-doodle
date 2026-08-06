"""Save EVERY kind of incoming attachment, each into media/<kind>/.

Use this for the one-pass test: send a photo, a voice note, a video, a GIF, a
document and a sticker from another phone and watch six files land.

    python save_all.py
"""

import wa_runner

if __name__ == "__main__":
    wa_runner.run(None, label="all media")
