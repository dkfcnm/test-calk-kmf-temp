"""Проверка окружения перед прогоном: Python, пакеты, ffmpeg с кодеком Opus, ключ доступа.
Печатает готовые команды для нарезки фрагмента и двух прогонов. К API Яндекса не обращается.

    python check_env.py                  — проверить и показать, что делать
    python check_env.py --install        — доустановить ffmpeg (Windows, winget)
    python check_env.py --cut "видео.mp4" [--seconds 60]   — нарезать фрагмент найденным ffmpeg
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

WINGET_CMD = "winget install --id Gyan.FFmpeg -e"
NEEDED = ("ffmpeg", "ffprobe")
rows: list[tuple[str, bool, str]] = []  # что проверяли, сошлось ли, что делать


def log(msg: str = "") -> None:
    print(msg, flush=True)


def add(name: str, good: bool, hint: str = "") -> bool:
    rows.append((name, good, hint))
    return good


def search_dirs() -> list[Path]:
    """Где искать ffmpeg, если его нет в PATH. Порядок — от вероятного к редкому."""
    if os.name != "nt":
        return [Path("/usr/bin"), Path("/usr/local/bin")]
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    dirs = [Path(r"C:\ffmpeg\bin"), Path(r"C:\Program Files\ffmpeg\bin"), Path.cwd(), Path(sys.argv[0]).parent]
    if local.name:
        dirs.append(local / "Microsoft" / "WinGet" / "Links")
        dirs += sorted(p.parent for p in (local / "Microsoft" / "WinGet" / "Packages").glob("*/**/ffmpeg.exe"))
    return dirs


def find_tools() -> Path | None:
    """Папка, где лежат обе программы: сначала PATH, затем обычные места установки."""
    if all(shutil.which(t) for t in NEEDED):
        return Path(shutil.which("ffmpeg")).parent
    suffix = ".exe" if os.name == "nt" else ""
    for d in search_dirs():
        if all((d / (t + suffix)).is_file() for t in NEEDED):
            return d
    return None


def has_opus(tools: Path) -> bool:
    out = subprocess.run([str(tools / "ffmpeg"), "-hide_banner", "-encoders"],
                         capture_output=True).stdout.decode("utf-8", "replace")
    return "libopus" in out


def install_ffmpeg() -> None:
    if os.name != "nt":
        sys.exit("Автоустановка сделана для Windows. В Linux: apt install ffmpeg; в macOS: brew install ffmpeg")
    if not shutil.which("winget"):
        sys.exit("Нет winget. Скачайте ffmpeg-release-full.7z с https://www.gyan.dev/ffmpeg/builds/ "
                 "и распакуйте в C:\\ffmpeg")
    log(f"Запускаю: {WINGET_CMD}")
    subprocess.run(WINGET_CMD.split(), check=False)
    log("\nУстановка завершена. ЗАКРОЙТЕ это окно и откройте новое — PATH обновляется только в новых окнах.")


def cut(video: Path, seconds: int, tools: Path) -> None:
    if not video.is_file():
        sys.exit(f"Файл не найден: {video}")
    out = video.with_name("фрагмент.mp4")
    subprocess.run([str(tools / "ffmpeg"), "-y", "-ss", "0", "-t", str(seconds), "-i", str(video),
                    "-c", "copy", str(out)], check=True)
    log(f"Готов фрагмент: {out}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Проверка окружения для yandex_video_translate.py")
    ap.add_argument("--install", action="store_true", help="доустановить ffmpeg (Windows, winget)")
    ap.add_argument("--cut", type=Path, metavar="ВИДЕО", help="нарезать фрагмент из видео")
    ap.add_argument("--seconds", type=int, default=60, help="длина фрагмента, с (по умолчанию 60)")
    args = ap.parse_args()

    if args.install:
        return install_ffmpeg()

    add(f"Python {sys.version_info.major}.{sys.version_info.minor}", sys.version_info >= (3, 9),
        "нужен Python 3.9 или новее: https://www.python.org/downloads/")
    import importlib.util
    missing = [p for p in ("requests", "numpy") if importlib.util.find_spec(p) is None]
    add("пакеты requests и numpy", not missing, f"pip install -U {' '.join(missing)}" if missing else "")

    tools = find_tools()
    in_path = bool(tools and all(shutil.which(t) for t in NEEDED))
    if tools:
        add(f"ffmpeg и ffprobe: {tools}", True)
        add("ffmpeg виден в PATH", in_path,
            f'разово в этом окне: set PATH=%PATH%;{tools}' if os.name == "nt" else f"добавьте в PATH: {tools}")
        add("кодек libopus в сборке ffmpeg", has_opus(tools),
            f"сборка неполная, поставьте полную: {WINGET_CMD}")
    else:
        add("ffmpeg и ffprobe", False, f"python {Path(sys.argv[0]).name} --install")

    add("ключ YC_API_KEY в окружении", bool(os.environ.get("YC_API_KEY")),
        'setx YC_API_KEY "ваш ключ" и новое окно терминала')

    log("\nПроверка окружения\n" + "-" * 60)
    for name, good, hint in rows:
        log(f"  {'OK  ' if good else 'НЕТ '} {name}")
        if not good and hint:
            log(f"       → {hint}")
    bad = [r for r in rows if not r[1]]
    log("-" * 60)

    if args.cut:
        if not tools:
            sys.exit("Нарезать нечем: ffmpeg не найден.")
        return cut(args.cut, args.seconds, tools)

    if bad:
        log(f"Не готово: {len(bad)}. Устраните по подсказкам выше и запустите проверку снова.")
        return
    prefix = "" if in_path else (f"set PATH=%PATH%;{tools}\n" if os.name == "nt" else f'PATH="$PATH:{tools}" ')
    log("Всё готово. Дальше:\n")
    log(f'{prefix}python {Path(sys.argv[0]).name} --cut "ваша лекция.mp4"')
    log('python yandex_video_translate.py "фрагмент.mp4" --fast')
    log('python yandex_video_translate.py "фрагмент.mp4" --fast --max-speed 3 --workdir fit_test_work')
    log("\nПосле прогонов пришлите файлы: фрагмент_work\\stt_raw.txt и fit_test_work\\tts_fit.json")


if __name__ == "__main__":
    main()
