import shutil
import subprocess

import pytest

from app.signals import long_silences, measure_signals, parse_freeze, parse_silence, signals_for_prompt


def test_parse_silence_and_open_ended_silence():
    err = ("[silencedetect] silence_start: 0\n[silencedetect] silence_end: 20.5 | silence_duration: 20.5\n"
           "[silencedetect] silence_start: 50.0\n")
    assert parse_silence(err, 60) == [{"start": 0.0, "end": 20.5}, {"start": 50.0, "end": 60}]


def test_parse_freeze():
    err = "lavfi.freezedetect.freeze_start: 5.0\nlavfi.freezedetect.freeze_end: 9.5\nlavfi.freezedetect.freeze_start: 30"
    assert parse_freeze(err, 40) == [{"start": 5.0, "end": 9.5}, {"start": 30.0, "end": 40}]


def test_prompt_text_and_long_silence_filter():
    sig = {"silences": [{"start": 0, "end": 1200}, {"start": 1300, "end": 1330}], "freezes": [], "notes": []}
    assert "00:00:00-00:20:00" in signals_for_prompt(sig)
    assert len(long_silences(sig, 150)) == 1


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs FFmpeg")
def test_measures_real_silence_and_freeze(tmp_path, settings):
    out = tmp_path / "t.mp4"
    # 25 s: 0-12 s silent audio and a still picture, then 13 s of tone and a moving picture
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "color=c=blue:size=160x120:rate=10:duration=12",
                    "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=13",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=13",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v];[2:a]atrim=duration=12[a0];[a0][3:a]concat=n=2:v=0:a=1[a]",
                    "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-c:a", "aac", str(out)], check=True)
    from dataclasses import replace
    s = replace(settings, silence_min_seconds=5.0, freeze_min_seconds=3.0)
    sig = measure_signals(out, 25.0, s)
    assert sig["errors"] == []
    assert sig["silences"] and sig["silences"][0]["start"] < 1 and 10 <= sig["silences"][0]["end"] <= 13
    assert sig["freezes"] and sig["freezes"][0]["start"] < 1 and 9 <= sig["freezes"][0]["end"] <= 13
