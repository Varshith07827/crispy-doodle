"""Save incoming PHOTOS at original quality.

winSpark today can only screenshot the on-screen thumbnail of a photo, so what
it stores is a lossy re-capture at whatever size WhatsApp happened to render —
and only while the window is visible. This saves the real file.

    python save_images.py
"""

import wa_runner

if __name__ == "__main__":
    wa_runner.run({"image"}, label="photos")
