"""Save incoming VOICE NOTES and audio files.

The one winSpark provably cannot do at all today: a voice note has no on-screen
pixels to screenshot, so constants.py states outright that voice notes "are
never obtainable this way". Here the opus/ogg file arrives intact.

Voice notes (PTT flag set) and shared music are separated — same protobuf, very
different things — and land in media/voice/ and media/audio/ respectively.

    python save_audio.py
"""

import wa_runner

if __name__ == "__main__":
    wa_runner.run({"voice", "audio"}, label="voice notes and audio")
