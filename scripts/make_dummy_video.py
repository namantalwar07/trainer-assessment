"""Create a dummy MP4 for mock-mode demos: python scripts/make_dummy_video.py [OUT_DIR]

Uses FFmpeg for a real tiny clip when installed; otherwise writes a minimal file with a valid MP4
'ftyp' header (enough for upload checks; mock mode skips FFmpeg validation).
"""

import shutil
import struct
import subprocess
import sys
from pathlib import Path

out = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_videos")
out.mkdir(parents=True, exist_ok=True)
names = ["UP_BUSINESSFUNDAMENTALS_TR001_S001_20261104.mp4", "UP_BUSINESSFUNDAMENTALS_TR001_S002_20261105.mp4",
         "BR_BUSINESSFUNDAMENTALS_TR002_S001_20261104.mp4"]
for i, name in enumerate(names):
    path = out / name
    if shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=8:size=320x240:rate=10",
                        "-f", "lavfi", "-i", f"sine=frequency={300 + i * 100}:duration=8", "-shortest",
                        "-c:v", "libx264", "-c:a", "aac", str(path)], check=True)
    else:
        ftyp = b"ftyp" + b"isom" + struct.pack(">I", 512) + b"isomiso2mp41"
        path.write_bytes(struct.pack(">I", 8 + len(ftyp) - 4) + ftyp + bytes([i + 1]) * 2048)
    print("wrote", path)
