"""Тестовые видео генерируются ffmpeg (lavfi) — реальные записи не нужны."""
import subprocess
from pathlib import Path


def make_media(dst: Path) -> None:
    def ff(*args):
        subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)

    video = ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=25"]
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    h264 = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    ff(*video, *tone, "-t", "20", "-ac", "2", *h264, "-c:a", "aac", "-b:a", "128k", str(dst / "тест видео.mp4"))
    ff(*video, *tone, "-t", "20", "-af", "pan=5.1|FL=c0|FR=c0|FC=c0|LFE=c0|BL=c0|BR=c0", *h264, "-c:a", "aac",
       str(dst / "surround.mkv"))
    ff(*video, *tone, "-t", "20", "-c:v", "libvpx-vp9", "-b:v", "200k", "-c:a", "libopus", str(dst / "clip.webm"))
    ff(*video, "-t", "5", *h264, str(dst / "noaudio.mp4"))
