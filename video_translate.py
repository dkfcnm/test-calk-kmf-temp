#!/usr/bin/env python3
"""
video_translate.py — закадровый перевод локального видеофайла с сохранением результата.
Аналог «Перевести и озвучить» из Яндекс Браузера, но для файлов на диске.

Конвейер (5 этапов):
  1. ffmpeg          — извлечь звук (16 кГц, моно)
  2. faster-whisper  — распознать речь с таймкодами (локально, без интернета)
  3. Google Translate (deep-translator) — перевести фразы (онлайн, бесплатно)
  4. Edge TTS        — озвучить перевод (онлайн, бесплатно)
  5. numpy + ffmpeg  — разложить озвучку по таймкодам, ускорить длинные фразы,
                       приглушить оригинал, собрать файл (видео не перекодируется)

Установка:  pip install -U faster-whisper edge-tts deep-translator numpy
            + ffmpeg 5+ в PATH (проверено на 6.1; Windows: winget install Gyan.FFmpeg)
Запуск:     python video_translate.py "лекция.mp4"
Правка:     исправьте поле "dst" в <имя>_work/translation.json и запустите ту же команду —
            распознавание и перевод возьмутся из кэша, переозвучатся только изменённые фразы.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

SR = 24000  # частота дорожки озвучки, Гц (родная частота Edge TTS)
SENTENCE_END = tuple(".!?…。！？")
DEFAULT_VOICES = {"ru": "ru-RU-DmitryNeural", "uk": "uk-UA-OstapNeural",
                  "en": "en-US-EmmaMultilingualNeural"}
ISO639_2 = {"ru": "rus", "en": "eng", "de": "deu", "fr": "fra", "es": "spa", "it": "ita",
            "zh": "zho", "ja": "jpn", "ko": "kor", "uk": "ukr", "kk": "kaz", "pt": "por",
            "tr": "tur", "pl": "pol"}
GOOGLE_CODE_FIX = {"zh": "zh-CN", "he": "iw"}  # коды языков Whisper → коды Google
TRANSLATE_CHUNK = 50  # фраз между сохранениями прогресса перевода


def log(msg: str = "", end: str = "\n") -> None:
    print(msg, end=end, flush=True)


# ─────────────────────────── ffmpeg / звук ───────────────────────────
def run(cmd: list[str], data: bytes | None = None) -> bytes:
    """Запустить внешнюю программу; при ошибке — исключение с концом stderr."""
    proc = subprocess.run(cmd, input=data, capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()[-2000:]
        raise RuntimeError(f"Ошибка {Path(cmd[0]).name}:\n{err}")
    return proc.stdout


def probe(path: Path) -> tuple[float, bool]:
    """Длительность файла (с) и наличие звуковой дорожки."""
    info = json.loads(run(["ffprobe", "-v", "error", "-show_entries",
                           "format=duration:stream=codec_type", "-of", "json", str(path)]))
    duration = float(info.get("format", {}).get("duration") or 0.0)
    has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    return duration, has_audio


def atempo(k: float) -> str:
    """Цепочка фильтров atempo (в старых ffmpeg один фильтр ограничен ×2)."""
    parts = []
    while k > 2.0:
        parts.append("atempo=2.0")
        k /= 2.0
    parts.append(f"atempo={k:.4f}")
    return ",".join(parts)


def to_pcm(src: Path | bytes, tempo: float = 1.0) -> np.ndarray:
    """Декодировать в int16 моно SR Гц; tempo > 1 — ускорить без изменения высоты голоса."""
    if isinstance(src, bytes):
        cmd = ["ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "pipe:0"]
    else:
        cmd = ["ffmpeg", "-v", "error", "-i", str(src)]
    if tempo > 1.001:
        cmd += ["-filter:a", atempo(tempo)]
    cmd += ["-f", "s16le", "-ar", str(SR), "-ac", "1", "pipe:1"]
    return np.frombuffer(run(cmd, src if isinstance(src, bytes) else None), dtype=np.int16)


def trim_silence(x: np.ndarray, threshold: int = 328, pad_s: float = 0.04) -> np.ndarray:
    """Срезать тишину по краям (порог −40 дБ), оставив запас pad_s."""
    loud = np.flatnonzero(np.abs(x.astype(np.int32)) > threshold)
    if loud.size == 0:
        return x[:0]
    pad = int(pad_s * SR)
    return x[max(loud[0] - pad, 0): loud[-1] + pad + 1]


def write_wav(path: Path, pcm: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.astype("<i2").tobytes())


# ─────────────────────────── 2. распознавание ───────────────────────────
def transcribe(wav: Path, model_name: str | None, device: str, language: str | None,
               batch: int) -> tuple[list[dict], str]:
    from faster_whisper import WhisperModel

    if device == "auto":
        import ctranslate2
        device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    model_name = model_name or ("large-v3-turbo" if device == "cuda" else "small")
    compute = "float16" if device == "cuda" else "int8"
    log(f"[2/5] Распознавание: модель {model_name}, {device}/{compute} "
        f"(при первом запуске модель скачивается)…")
    model = WhisperModel(model_name, device=device, compute_type=compute)
    if batch > 0:
        from faster_whisper import BatchedInferencePipeline
        segments, info = BatchedInferencePipeline(model).transcribe(
            str(wav), language=language, batch_size=batch, without_timestamps=False)
    else:
        segments, info = model.transcribe(str(wav), language=language, beam_size=5,
                                          vad_filter=True, condition_on_previous_text=False)
    result = []
    for s in segments:
        if s.text.strip():
            result.append({"start": round(s.start, 3), "end": round(s.end, 3),
                           "text": s.text.strip()})
        if info.duration:
            log(f"\r      {min(s.end / info.duration, 1.0):6.1%}", end="")
    log()
    log(f"      язык: {info.language} (вероятность {info.language_probability:.2f}), "
        f"сегментов: {len(result)}")
    return result, info.language


def merge_phrases(segs: list[dict], max_len: float = 12.0, max_gap: float = 0.8) -> list[dict]:
    """Склеить сегменты, разрезанные посреди предложения: перевод целой фразы точнее."""
    out: list[dict] = []
    for s in segs:
        if out:
            p = out[-1]
            finished = p["text"].rstrip("\"'»”) ").endswith(SENTENCE_END)
            if (not finished and s["start"] - p["end"] <= max_gap
                    and s["end"] - p["start"] <= max_len):
                p["end"] = s["end"]
                p["text"] = f'{p["text"]} {s["text"]}'
                continue
        out.append(dict(s))
    return out


# ─────────────────────────── 3. перевод ───────────────────────────
def translate_texts(texts: list[str], src: str, dst: str, workers: int = 4) -> list[str]:
    from deep_translator import GoogleTranslator
    from deep_translator.constants import GOOGLE_LANGUAGES_TO_CODES

    src = GOOGLE_CODE_FIX.get(src, src)
    if src not in GOOGLE_LANGUAGES_TO_CODES.values():
        src = "auto"
    dst = GOOGLE_CODE_FIX.get(dst, dst)

    def one(text: str) -> str:
        for attempt in range(5):
            try:
                return GoogleTranslator(source=src, target=dst).translate(text) or ""
            except Exception as exc:  # сеть, лимит 429, временные сбои
                if attempt == 4:
                    raise RuntimeError(f"Не удалось перевести «{text[:60]}»: {exc}") from exc
                time.sleep(2 ** attempt)
        return ""

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, texts))


# ─────────────────────────── 4. озвучка ───────────────────────────
async def synthesize(jobs: list[tuple[str, Path]], voice: str, rate: str,
                     parallel: int = 4) -> None:
    import edge_tts

    sem = asyncio.Semaphore(parallel)
    done = 0

    async def one(text: str, path: Path) -> None:
        nonlocal done
        async with sem:
            for attempt in range(4):
                try:
                    part = path.with_suffix(".part")
                    await edge_tts.Communicate(text, voice, rate=rate).save(str(part))
                    part.replace(path)
                    break
                except Exception as exc:
                    if attempt == 3:
                        raise RuntimeError(f"Edge TTS не озвучил «{text[:60]}»: {exc}") from exc
                    await asyncio.sleep(2 ** attempt)
        done += 1
        log(f"\r      {done}/{len(jobs)}", end="")

    await asyncio.gather(*(one(t, p) for t, p in jobs))
    log()


# ─────────────────────────── 5. сборка ───────────────────────────
def build_voice_track(items: list[dict], files: list[Path], total: float,
                      max_speed: float) -> tuple[np.ndarray, dict]:
    """Разложить озвучку по таймкодам. Фраза стартует в своё время или сразу после предыдущей;
    если не помещается до начала следующей — ускоряется, но не больше max_speed."""
    track = np.zeros(int((total + 0.5) * SR), dtype=np.int16)
    cursor = 0.0
    stats = {"sped": 0, "max_k": 1.0, "late": 0, "overflow": 0.0}
    for i, (item, f) in enumerate(zip(items, files)):
        pcm = trim_silence(to_pcm(f))
        if pcm.size == 0:
            continue
        start = max(item["start"], cursor)
        next_start = items[i + 1]["start"] if i + 1 < len(items) else total
        window = max(next_start - start, 0.2)
        k = min(max(pcm.size / SR / window, 1.0), max_speed)
        if k > 1.02:
            pcm = to_pcm(pcm.tobytes(), tempo=k)
            stats["sped"] += 1
            stats["max_k"] = max(stats["max_k"], k)
        a = int(round(start * SR))
        b = a + pcm.size
        if b > track.size:
            track = np.concatenate([track, np.zeros(b - track.size, dtype=np.int16)])
        track[a:b] = pcm
        cursor = b / SR + 0.05
        stats["late"] += int(start - item["start"] > 0.5)
    stats["overflow"] = max(cursor - 0.05 - total, 0.0)
    return track, stats


def mux(video: Path, voice_wav: Path, out: Path, orig_volume: float, keep_original: bool,
        src_lang: str, dst_lang: str) -> None:
    ext = out.suffix.lower()
    audio_codec = ["libopus", "-b:a", "128k"] if ext == ".webm" else ["aac", "-b:a", "192k"]
    graph = (f"[0:a:0]volume={orig_volume}[bg];"
             f"[bg][1:a]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
             f"alimiter=limit=0.95:level=0[mix]")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(voice_wav),
           "-filter_complex", graph, "-map", "0:v:0?", "-map", "[mix]"]
    if keep_original:
        cmd += ["-map", "0:a:0"]
    cmd += ["-c:v", "copy", "-c:a", *audio_codec,
            "-metadata:s:a:0", f"language={ISO639_2.get(dst_lang, 'und')}",
            "-metadata:s:a:0", "title=Перевод", "-metadata:s:a:0", "handler_name=Перевод",
            "-disposition:a:0", "default"]
    if keep_original:
        cmd += ["-metadata:s:a:1", f"language={ISO639_2.get(src_lang, 'und')}",
                "-metadata:s:a:1", "title=Оригинал", "-metadata:s:a:1", "handler_name=Оригинал",
                "-disposition:a:1", "0"]
    if ext in (".mp4", ".m4v", ".mov"):  # в mp4 имя дорожки плееры берут из handler_name
        cmd += ["-movflags", "+faststart"]
    run(cmd + [str(out)])


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(path: Path, items: list[dict]) -> None:
    rows = [it for it in items if it["dst"].strip()]
    blocks = [f"{n}\n{srt_time(it['start'])} --> {srt_time(it['end'])}\n{it['dst'].strip()}\n"
              for n, it in enumerate(rows, 1)]
    path.write_text("\n".join(blocks), encoding="utf-8")


# ─────────────────────────── служебное ───────────────────────────
def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"Файл {path} повреждён ({exc}). Исправьте его или удалите, чтобы пересчитать.")


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def default_output(src: Path, dst: str) -> Path:
    ext = src.suffix.lower()
    return src.with_name(f"{src.stem}_{dst}{ext if ext in ('.mp4', '.m4v', '.mov', '.mkv') else '.mkv'}")


def tts_name(voice: str, rate: str, text: str) -> str:
    return hashlib.sha1(f"{voice}|{rate}|{text}".encode("utf-8")).hexdigest()[:16] + ".mp3"


def check_environment() -> None:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        sys.exit("Не найден ffmpeg/ffprobe в PATH. Windows: winget install Gyan.FFmpeg; "
                 "macOS: brew install ffmpeg; Linux: sudo apt install ffmpeg")
    missing = [pkg for mod, pkg in (("faster_whisper", "faster-whisper"), ("edge_tts", "edge-tts"),
                                    ("deep_translator", "deep-translator"))
               if importlib.util.find_spec(mod) is None]
    if missing:
        sys.exit(f"Не установлены пакеты: {', '.join(missing)}. Выполните: pip install -U {' '.join(missing)}")


# ─────────────────────────── главный сценарий ───────────────────────────
def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Закадровый перевод локального видеофайла.")
    ap.add_argument("input", type=Path, help="исходный видеофайл")
    ap.add_argument("-o", "--output", type=Path,
                    help="результат (по умолчанию <имя>_<язык>.mp4|.mkv рядом с исходником)")
    ap.add_argument("--src", help="язык оригинала: en, de, zh… (по умолчанию — автоопределение)")
    ap.add_argument("--dst", default="ru", help="язык перевода (по умолчанию ru)")
    ap.add_argument("--model", help="модель Whisper: small | medium | large-v3 | large-v3-turbo "
                                    "(по умолчанию: CPU → small, GPU → large-v3-turbo)")
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda", "auto"],
                    help="cuda — видеокарта NVIDIA (нужны cuBLAS для CUDA 12 и cuDNN 9)")
    ap.add_argument("--batch", type=int, default=0,
                    help="пакетное распознавание, напр. 8: быстрее, но требует больше памяти")
    ap.add_argument("--voice", help="голос Edge TTS (по умолчанию ru-RU-DmitryNeural; "
                                    "женский — ru-RU-SvetlanaNeural)")
    ap.add_argument("--rate", default="+10%", help="темп озвучки: +0%%, +10%%, +20%%…")
    ap.add_argument("--orig-volume", type=float, default=0.25,
                    help="громкость оригинала под переводом, 0…1 (по умолчанию 0.25)")
    ap.add_argument("--max-speed", type=float, default=1.5,
                    help="предельное ускорение фразы для попадания в тайминг (по умолчанию 1.5)")
    ap.add_argument("--no-original-track", action="store_true",
                    help="не добавлять оригинальную дорожку второй")
    ap.add_argument("--workdir", type=Path, help="папка кэша (по умолчанию <имя>_work)")
    args = ap.parse_args(argv)

    if not re.fullmatch(r"[+-]\d{1,3}%", args.rate):
        sys.exit("--rate: формат +10% или -5%")
    if not 0.0 <= args.orig_volume <= 1.0:
        sys.exit("--orig-volume: число от 0 до 1")
    if not 1.0 <= args.max_speed <= 3.0:
        sys.exit("--max-speed: число от 1 до 3")
    check_environment()

    t0 = time.time()
    src_file = args.input.resolve()
    if not src_file.is_file():
        sys.exit(f"Файл не найден: {src_file}")
    out = (args.output or default_output(src_file, args.dst)).resolve()
    if out == src_file:
        sys.exit("Выходной файл совпадает с исходным — укажите другое имя в -o.")
    voice = args.voice or DEFAULT_VOICES.get(args.dst)
    if not voice:
        sys.exit(f"Для языка «{args.dst}» укажите --voice (список голосов: edge-tts --list-voices)")
    duration, has_audio = probe(src_file)
    if not has_audio:
        sys.exit("В файле нет звуковой дорожки — переводить нечего.")
    work = (args.workdir or src_file.with_name(src_file.stem + "_work")).resolve()
    (work / "tts").mkdir(parents=True, exist_ok=True)

    # 1–2. Распознавание (кэш: transcript.json)
    tr_path = work / "transcript.json"
    key = {"model": args.model or "auto", "device": args.device, "src": args.src or "auto"}
    cached = load_json(tr_path)
    if cached and cached.get("key") == key:
        segs, lang = cached["segments"], cached["language"]
        log(f"[1-2/5] Распознавание: из кэша (сегментов: {len(segs)}, язык: {lang})")
    else:
        log("[1/5] Извлечение звука…")
        wav16 = work / "audio16k.wav"
        run(["ffmpeg", "-y", "-v", "error", "-i", str(src_file), "-map", "0:a:0", "-vn",
             "-ac", "1", "-ar", "16000", str(wav16)])
        segs, lang = transcribe(wav16, args.model, args.device, args.src, args.batch)
        save_json(tr_path, {"key": key, "language": lang, "segments": segs})
    if not segs:
        sys.exit("Речь не обнаружена.")
    if lang == args.dst and not args.src:
        sys.exit(f"Язык видео определён как «{lang}» — перевод не нужен. "
                 f"Если это ошибка, укажите язык оригинала: --src en")
    duration = duration or segs[-1]["end"] + 1.0

    # 3. Перевод (кэш и ручная правка: translation.json)
    phrases = merge_phrases(segs)
    tl_path = work / "translation.json"
    cached = load_json(tl_path)
    if (cached and cached.get("dst") == args.dst
            and [x["src"] for x in cached["items"]] == [p["text"] for p in phrases]):
        items = cached["items"]
        log(f"[3/5] Перевод: из кэша (фраз: {len(items)}, ручные правки сохранены)")
    else:
        memo_path = work / f"mt_cache_{args.dst}.json"  # машинный перевод, сохраняется по пачкам
        memo = load_json(memo_path) or {}
        need = list(dict.fromkeys(p["text"] for p in phrases if p["text"] not in memo))
        log(f"[3/5] Перевод: новых фраз {len(need)} из {len(phrases)} ({lang} → {args.dst})…")
        for i in range(0, len(need), TRANSLATE_CHUNK):
            chunk = need[i:i + TRANSLATE_CHUNK]
            memo.update(zip(chunk, translate_texts(chunk, lang, args.dst)))
            save_json(memo_path, memo)
            log(f"\r      {i + len(chunk)}/{len(need)}", end="")
        log()
        items = [{"start": p["start"], "end": p["end"], "src": p["text"], "dst": memo[p["text"]]}
                 for p in phrases]
        save_json(tl_path, {"dst": args.dst, "items": items})

    # 4. Озвучка (кэш: tts/<хэш>.mp3)
    speak = [it for it in items if re.search(r"\w", it["dst"])]
    files = [work / "tts" / tts_name(voice, args.rate, it["dst"]) for it in speak]
    todo = list({f: (it["dst"], f) for it, f in zip(speak, files) if not f.exists()}.values())
    log(f"[4/5] Озвучка: новых фраз {len(todo)} из {len(speak)} (голос {voice}, темп {args.rate})…")
    if todo:
        asyncio.run(synthesize(todo, voice, args.rate))

    # 5. Сборка
    log("[5/5] Сведение дорожек и сборка файла…")
    track, stats = build_voice_track(speak, files, duration, args.max_speed)
    voice_wav = work / "voice.wav"
    write_wav(voice_wav, track)
    mux(src_file, voice_wav, out, args.orig_volume, not args.no_original_track, lang, args.dst)
    srt = out.with_suffix(".srt")
    write_srt(srt, items)

    log(f"\nГотово за {time.time() - t0:.0f} с")
    log(f"  видео:     {out}")
    log(f"  субтитры:  {srt}")
    log(f"  правка:    {tl_path}")
    log(f"  фраз: {len(speak)}; ускорено: {stats['sped']} (макс. ×{stats['max_k']:.2f}); "
        f"начато позже оригинала >0,5 с: {stats['late']}")
    if stats["overflow"] > 0:
        log(f"  ВНИМАНИЕ: озвучка длиннее видео на {stats['overflow']:.1f} с — хвост обрезан. "
            f"Сократите последние фразы в translation.json или увеличьте --max-speed.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nПрервано. Повторный запуск продолжит с места остановки (кэш в папке *_work).")
    except RuntimeError as exc:
        sys.exit(f"\nОШИБКА: {exc}")
