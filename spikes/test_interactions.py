"""SPIKE (run once on ONE approved sample video before real use):

    GEMINI_API_KEY=... MOCK_GEMINI=false python spikes/test_interactions.py path/to/sample.mp4

Confirms the Interactions API call shape from the 4 Nov 2026 docs works with gemini-3.8-flash and
shows which fields carry the status, text and token usage, because app/gemini/live.py parses them
defensively. Prints the raw interaction object so the parser can be tightened.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.gemini.live import LiveEngine  # noqa: E402

if len(sys.argv) != 2:
    sys.exit(__doc__)
s = Settings.from_env()
if s.mock_gemini:
    sys.exit("Set MOCK_GEMINI=false and GEMINI_API_KEY to run this spike.")
eng = LiveEngine(s)
h = eng.prepare_video(Path(sys.argv[1]), "video/mp4")
try:
    inter = eng.client.interactions.create(
        model=eng.model, background=True,
        input=[{"type": "video", "uri": h.uri, "mime_type": h.mime_type, "processing": "agentic"},
               {"type": "text", "text": "Summarise this video in 3 sentences with timestamps."}])
    print("CREATED:", inter)
    import time
    while str(getattr(inter, "status", "")).lower() not in {"completed", "failed", "cancelled"}:
        time.sleep(10)
        inter = eng.client.interactions.get(inter.id)
        print("status:", getattr(inter, "status", None))
    print("FINAL:", inter)
finally:
    eng.release(h)
