"""Проверка окружения перед прогоном: Python, пакеты, ffmpeg с кодеком Opus, ключ доступа.
Печатает готовые команды для нарезки фрагмента и прогона. К API Яндекса не обращается.

    python check_env.py                  — проверить и показать, что делать
    python check_env.py --install        — доустановить ffmpeg (Windows, winget)
    python check_env.py --cut "видео.mp4" [--start 0] [--seconds 60]  — нарезать фрагмент
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

WINGET_CMD = "winget install --id Gyan.FFmpeg -e"
TOOLS = ("ffmpeg", "ffprobe")
WINDOWS = os.name == "nt"
EXE = ".exe" if WINDOWS else ""
PY = "python" if WINDOWS else "python3"
SELF = sys.argv[0] or "check_env.py"  # как пользователь запустил этот скрипт — так и подсказываем


class Check(NamedTuple):
    name: str
    ok: bool
    hint: str = ""
    soft: bool = False  # мягкий: работать можно, но с оговоркой из подсказки


checks: list[Check] = []


def log(msg: str = "") -> None:
    print(msg, flush=True)


def note(name: str, ok: bool, hint: str = "", soft: bool = False) -> None:
    checks.append(Check(name, ok, hint, soft))


def key_ok(value: str | None) -> bool:
    """Как в get_key() основной программы: пробелы ключом не считаются."""
    return bool((value or "").strip())


def search_dirs() -> list[Path]:
    """Где искать ffmpeg, если его нет в PATH. Порядок — от вероятного к редкому."""
    if not WINDOWS:
        return [Path("/usr/local/bin"), Path("/opt/homebrew/bin")]
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    dirs = [Path(r"C:\ffmpeg\bin"), Path(r"C:\Program Files\ffmpeg\bin"), Path.cwd(), Path(sys.argv[0]).parent]
    if local.name:
        dirs.append(local / "Microsoft" / "WinGet" / "Links")
        dirs += sorted(p.parent for p in (local / "Microsoft" / "WinGet" / "Packages").glob("*/**/ffmpeg.exe"))
    return dirs


def find_in_dirs(dirs: list[Path]) -> Path | None:
    """Первая папка списка, где лежат обе программы."""
    for d in dirs:
        if all((d / (t + EXE)).is_file() for t in TOOLS):
            return d
    return None


def find_tools() -> tuple[Path | None, bool]:
    """Папка с ffmpeg и ffprobe и признак «видны в PATH»."""
    if all(shutil.which(t) for t in TOOLS):
        return Path(str(shutil.which("ffmpeg"))).parent, True
    return find_in_dirs(search_dirs()), False


def has_opus(tools: Path) -> bool:
    try:
        p = subprocess.run([str(tools / ("ffmpeg" + EXE)), "-hide_banner", "-encoders"], capture_output=True)
    except OSError:
        return False
    return p.returncode == 0 and "libopus" in p.stdout.decode("utf-8", "replace")


def path_hints(tools: Path) -> list[str]:
    """Как добавить папку в PATH текущего окна — в обеих оболочках Windows."""
    if not WINDOWS:
        return [f'export PATH="$PATH:{tools}"']
    return [f"cmd:        set PATH=%PATH%;{tools}", f'PowerShell: $env:PATH += ";{tools}"']


def install_ffmpeg() -> None:
    if not WINDOWS:
        sys.exit("Автоустановка сделана для Windows. Linux: apt install ffmpeg; macOS: brew install ffmpeg")
    if not shutil.which("winget"):
        sys.exit("Нет winget. Скачайте ffmpeg-release-full.7z с https://www.gyan.dev/ffmpeg/builds/ "
                 "и распакуйте в C:\\ffmpeg")
    log(f"Запускаю: {WINGET_CMD}")
    subprocess.run(WINGET_CMD.split(), check=False)
    log("\nУстановка завершена. ЗАКРОЙТЕ это окно и откройте новое — PATH обновляется только в новых окнах.")


def cut(video: Path, start: int, seconds: int, tools: Path) -> Path:
    if not video.is_file():
        sys.exit(f"Файл не найден: {video}")
    out = (video.parent / "фрагмент.mp4").resolve()
    subprocess.run([str(tools / ("ffmpeg" + EXE)), "-y", "-ss", str(start), "-t", str(seconds), "-i", str(video),
                    "-c", "copy", str(out)], check=True)
    return out


def next_steps(tools: Path, in_path: bool, fragment: Path | None = None) -> None:
    """Команды следующего шага: с поправкой на PATH и на реальный путь фрагмента."""
    src = f'"{fragment}"' if fragment else '"фрагмент.mp4"'
    work = (fragment.parent / "фрагмент_work") if fragment else Path("фрагмент_work")
    log("\nДальше:\n")
    if not in_path:
        log("  сначала добавьте ffmpeg в PATH этого окна —")
        for h in path_hints(tools):
            log("    " + h)
    if not fragment:
        log(f'  {PY} {SELF} --cut "ваша лекция.mp4"')
    log(f'  {PY} yandex_video_translate.py {src} --fast')
    log(f"\nПосле прогона пришлите два файла:\n  {work / 'stt_raw.txt'}\n  {work / 'tts_fit.json'}")
    log(f"\nЕсли в tts_fit.json список \"phrases\" пуст — в этом фрагменте не было фраз, не помещавшихся\n"
        f"в тайминг. Возьмите другой участок: {PY} {SELF} --cut \"ваша лекция.mp4\" --start 600")


def run_checks() -> tuple[Path | None, bool]:
    note(f"Python {sys.version_info.major}.{sys.version_info.minor}", sys.version_info >= (3, 9),
         "нужен Python 3.9 или новее: https://www.python.org/downloads/")
    import importlib.util
    missing = [p for p in ("requests", "numpy") if importlib.util.find_spec(p) is None]
    note("пакеты requests и numpy", not missing, f"pip install -U {' '.join(missing)}" if missing else "")

    tools, in_path = find_tools()
    if tools:
        note(f"ffmpeg и ffprobe: {tools}", True)
        note("ffmpeg виден в PATH", in_path, " | ".join(path_hints(tools)), soft=True)
        note("кодек libopus в сборке ffmpeg", has_opus(tools), f"сборка неполная, поставьте полную: {WINGET_CMD}")
    else:
        note("ffmpeg и ffprobe", False, f"{PY} {SELF} --install")

    main_py = Path(SELF).resolve().parent / "yandex_video_translate.py"
    note(f"основная программа рядом: {main_py.name}", main_py.is_file(),
         f"положите yandex_video_translate.py в {main_py.parent}")

    note("ключ YC_API_KEY в окружении", key_ok(os.environ.get("YC_API_KEY")),
         'setx YC_API_KEY "ваш ключ" и новое окно терминала' if WINDOWS
         else 'export YC_API_KEY="ваш ключ" в ~/.bashrc или ~/.zshrc')
    return tools, in_path


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # кириллица в cmd не должна ронять вывод
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Проверка окружения для yandex_video_translate.py")
    ap.add_argument("--install", action="store_true", help="доустановить ffmpeg (Windows, winget)")
    ap.add_argument("--cut", type=Path, metavar="ВИДЕО", help="нарезать фрагмент из видео")
    ap.add_argument("--start", type=int, default=0, help="начало фрагмента, с (по умолчанию 0)")
    ap.add_argument("--seconds", type=int, default=60, help="длина фрагмента, с (по умолчанию 60)")
    args = ap.parse_args()

    if args.install:
        return install_ffmpeg()

    tools, in_path = run_checks()
    log("\nПроверка окружения\n" + "-" * 60)
    for c in checks:
        log(f"  {'OK  ' if c.ok else 'НЕТ '} {c.name}")
        if not c.ok and c.hint:
            log(f"       -> {c.hint}")
    blockers = [c for c in checks if not c.ok and not c.soft]  # мягкое (PATH) лечится прямо в командах ниже
    log("-" * 60)

    fragment = None
    if args.cut:
        if not tools:
            sys.exit("Нарезать нечем: ffmpeg не найден.")
        fragment = cut(args.cut, args.start, args.seconds, tools)
        log(f"Готов фрагмент: {fragment}")

    if blockers:
        log(f"Не готово: {len(blockers)}. Устраните по подсказкам выше и запустите проверку снова.")
        sys.exit(1)
    next_steps(tools, in_path, fragment)


if __name__ == "__main__":
    main()
