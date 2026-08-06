"""Save incoming VIDEOS, GIFs and round video notes.

GIFs on WhatsApp are mp4 videos with a gifPlayback flag, so they come through
this script rather than as image files. Round "video notes" (ptvMessage) are
saved separately under media/video_note/.

    python save_videos.py
"""

import wa_runner

if __name__ == "__main__":
    wa_runner.run({"video", "video_note"}, label="videos")
