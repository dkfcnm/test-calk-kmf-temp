#!/usr/bin/env python3
"""
yandex_video_translate.py — закадровый перевод локального видео через сервисы Яндекса.

Конвейер (5 этапов, один запуск):
  1. ffmpeg                — извлечь звук
  2. SpeechKit (STT v3)    — распознать речь с таймкодами (асинхронно, без бакета: до 60 МБ звука).
                             По умолчанию — отложенный режим: ≈в 4 раза дешевле, результат до 24 ч;
                             --fast — стандартный режим (обычно минуты)
                             [альтернатива: --asr whisper — локально, бесплатно]
  3. Yandex Translate (v2) — перевести фразы (пачками, с глоссарием)
  4. SpeechKit (TTS v3)    — озвучить; длинные фразы пересинтезируются с ограничением длительности
  5. numpy + ffmpeg        — разложить озвучку по таймкодам, приглушить оригинал, собрать файл
                             (видео не перекодируется; оригинал — второй дорожкой; .srt рядом)

Установка:  pip install -U requests numpy        (+ faster-whisper — только для --asr whisper)
            + ffmpeg 5+ в PATH (Windows: winget install Gyan.FFmpeg)
Ключ:       переменная окружения YC_API_KEY — API-ключ сервисного аккаунта (AI Studio → «Создать API-ключ»).
            В файлы и логи не пишется. YC_FOLDER_ID — не обязателен.
Проверка:   python yandex_video_translate.py --check        (≈0,35 ₽: перевод + синтез + распознавание)
Запуск:     python yandex_video_translate.py "лекция.mp4"          → отправит звук и выйдет;
            ту же команду запустите позже — программа заберёт результат (хранится 3 суток)
            и доделает перевод, озвучку и сборку. Ждать в окне: --wait. Быстро: --fast.
Правка:     исправьте "dst" в <имя>_work/translation.json и запустите ту же команду —
            платно переозвучатся только изменённые фразы.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

SR = 24000  # частота дорожки озвучки, Гц
SENTENCE_END = tuple(".!?…。！？")
ISO639_2 = {"ru": "rus", "en": "eng", "de": "deu", "fr": "fra", "es": "spa", "it": "ita",
            "zh": "zho", "ja": "jpn", "ko": "kor", "uk": "ukr", "kk": "kaz", "pt": "por",
            "tr": "tur", "pl": "pol", "uz": "uzb", "he": "heb"}
STT_LANG = {"en": "en-US", "ru": "ru-RU", "de": "de-DE", "fr": "fr-FR", "es": "es-ES", "it": "it-IT"}

TRANSLATE_URL = "https://translate.api.cloud.yandex.net/translate/v2/translate"
TTS_URL = "https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis"
STT_URL = "https://stt.api.cloud.yandex.net/stt/v3/recognizeFileAsync"
GETREC_URL = "https://stt.api.cloud.yandex.net/stt/v3/getRecognition"
OPERATION_URL = "https://operation.api.cloud.yandex.net/operations"

# Тарифы с НДС на 11.09.2026 (AI Studio → Правила тарификации) — только для оценки стоимости
PRICE_STT_15S, PRICE_STT_DEFERRED_15S = 0.1515, 0.0381
PRICE_TR_1M, PRICE_TTS_UNIT = 500.4, 0.1626
STT_KEEP_HOURS = 70      # результаты распознавания хранятся 3 суток — берём с запасом
TTS_LIMIT = 250          # символов в обычном запросе синтеза API v3
TR_BATCH_CHARS = 9000    # запас к лимиту 10 000 символов на запрос перевода
STT_MAX_BYTES = 60 * 1024 * 1024
MAX_SEG = 12.0           # с — длиннее режем по паузам между словами
COST = {"stt_sec": 0.0, "tr_chars": 0, "tts_units": 0}


def log(msg: str = "", end: str = "\n") -> None:
    print(msg, end=end, flush=True)


# ─────────────────────────── ffmpeg / звук ───────────────────────────
def run(cmd: list[str], data: bytes | None = None) -> bytes:
    proc = subprocess.run(cmd, input=data, capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()[-2000:]
        raise RuntimeError(f"Ошибка {Path(cmd[0]).name}:\n{err}")
    return proc.stdout


def probe(path: Path) -> tuple[float, bool, bool]:
    """Длительность, наличие звука и признак уже переведённого файла (дорожка «Перевод»)."""
    info = json.loads(run(["ffprobe", "-v", "error", "-show_entries",
                           "format=duration:stream=codec_type:stream_tags=title,handler_name",
                           "-of", "json", str(path)]))
    duration = float(info.get("format", {}).get("duration") or 0.0)
    audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    ours = any("Перевод" in (s.get("tags", {}).get("title", ""), s.get("tags", {}).get("handler_name", ""))
               for s in audio)
    return duration, bool(audio), ours


def atempo(k: float) -> str:
    parts = []
    while k > 2.0:
        parts.append("atempo=2.0")
        k /= 2.0
    parts.append(f"atempo={k:.4f}")
    return ",".join(parts)


def speed_up(pcm: np.ndarray, k: float) -> np.ndarray:
    """Ускорить без изменения высоты голоса (ffmpeg atempo)."""
    out = run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "pipe:0",
               "-filter:a", atempo(k), "-f", "s16le", "-ar", str(SR), "-ac", "1", "pipe:1"],
              pcm.astype("<i2").tobytes())
    return np.frombuffer(out, dtype="<i2")


def trim_silence(x: np.ndarray, threshold: int = 328, pad_s: float = 0.04) -> np.ndarray:
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


# ─────────────────────────── HTTP к Yandex Cloud ───────────────────────────
class YCAuthError(RuntimeError):
    """Отказ из-за ключа, прав или платёжного аккаунта — повтор не поможет."""


def yc_request(method: str, url: str, key: str, *, body: dict | None = None,
               params: dict | None = None, timeout: int = 300) -> bytes:
    """Запрос с API-ключом; повторы при сетевых сбоях, 429 и 5xx."""
    import requests

    headers = {"Authorization": f"Api-Key {key}"}
    if os.environ.get("YC_FOLDER_ID"):
        headers["x-folder-id"] = os.environ["YC_FOLDER_ID"]
    problem = ""
    for attempt in range(6):
        try:
            r = requests.request(method, url, headers=headers, json=body, params=params,
                                 timeout=timeout)
        except requests.RequestException as exc:
            problem = f"сеть: {exc.__class__.__name__}"
        else:
            if r.status_code == 200:
                return r.content
            text = r.text.replace(key, "***")[:400]
            if r.status_code in (401, 403):
                raise YCAuthError(f"Яндекс отклонил ключ или права (HTTP {r.status_code}): {text}\n"
                                  f"Проверьте YC_API_KEY, роль ai.editor у сервисного аккаунта "
                                  f"и активный платёжный аккаунт.")
            if r.status_code != 429 and r.status_code < 500:
                raise RuntimeError(f"Ошибка запроса {url.split('/')[-1]} (HTTP {r.status_code}): {text}")
            problem = f"HTTP {r.status_code}"
        if attempt < 5:
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"Сервис недоступен после 6 попыток ({problem}): {url}")


def iter_json(raw: str):
    """Поток REST-ответов SpeechKit: JSON-объекты подряд (построчно или «склеенные»)."""
    dec, i, n = json.JSONDecoder(), 0, len(raw)
    while i < n:
        while i < n and raw[i] in " \t\r\n,[]":
            i += 1
        if i >= n:
            break
        obj, i = dec.raw_decode(raw, i)
        yield obj


# ─────────────────────────── 2. распознавание ───────────────────────────
def split_long(text: str, start: float, end: float, words: list[dict]) -> list[dict]:
    """Длинное высказывание (> MAX_SEG) режем по паузам между словами."""
    if end - start <= MAX_SEG or not words:
        return [{"start": start, "end": end, "text": text}]
    out, cur, cur_start = [], [], None
    for i, w in enumerate(words):
        ws, we = int(w.get("startTimeMs", 0)) / 1000, int(w.get("endTimeMs", 0)) / 1000
        if cur_start is None:
            cur_start = ws
        cur.append(w.get("text", ""))
        nxt = int(words[i + 1].get("startTimeMs", 0)) / 1000 if i + 1 < len(words) else None
        gap = (nxt - we) if nxt is not None else 0.0
        if nxt is None or (we - cur_start >= MAX_SEG - 2 and gap >= 0.25) or we - cur_start >= MAX_SEG:
            out.append({"start": round(cur_start, 3), "end": round(we, 3), "text": " ".join(cur).strip()})
            cur, cur_start = [], None
    return [s for s in out if s["text"]]


def stt_submit(audio: Path, key: str, src: str | None, model: str) -> str:
    data = audio.read_bytes()
    if len(data) > STT_MAX_BYTES:
        sys.exit("Звук длиннее лимита SpeechKit для загрузки в запросе (60 МБ ≈ 4 ч). "
                 "Разрежьте видео или используйте --asr whisper.")
    lang_code = STT_LANG.get(src, src) if src else "auto"
    body = {"content": base64.b64encode(data).decode("ascii"),
            "recognition_model": {
                "model": model,
                "audio_format": {"container_audio": {"container_audio_type": "OGG_OPUS"}},
                "text_normalization": {"text_normalization": "TEXT_NORMALIZATION_ENABLED",
                                       "literature_text": True},
                "language_restriction": {"restriction_type": "WHITELIST",
                                         "language_code": [lang_code]}}}
    return json.loads(yc_request("POST", STT_URL, key, body=body))["id"]


def stt_ready(op_id: str, key: str) -> bool:
    st = json.loads(yc_request("GET", f"{OPERATION_URL}/{op_id}", key))
    if st.get("error"):
        raise RuntimeError(f"SpeechKit: ошибка распознавания: {st['error']}")
    return bool(st.get("done"))


def stt_result(op_id: str, key: str, src: str | None,
               raw_path: Path | None = None) -> tuple[list[dict], str | None]:
    raw = yc_request("GET", GETREC_URL, key, params={"operation_id": op_id}).decode("utf-8")
    if raw_path:  # дословный ответ: по нему сверяют форматы API с эмуляцией в tests/test_yandex.py
        raw_path.write_bytes(raw.encode("utf-8"))  # байтами: текстовая запись портит концы строк в Windows
    finals, refined, langs = [], {}, {}
    for obj in iter_json(raw):
        res = obj.get("result", obj)
        if "error" in res:
            raise RuntimeError(f"SpeechKit: {res['error']}")
        if res.get("final", {}).get("alternatives"):
            idx = res.get("audioCursors", {}).get("finalIndex", str(len(finals)))
            finals.append((str(idx), res["final"]["alternatives"][0]))
        elif res.get("finalRefinement", {}).get("normalizedText", {}).get("alternatives"):
            fr = res["finalRefinement"]
            refined[str(fr.get("finalIndex", len(refined)))] = fr["normalizedText"]["alternatives"][0]
    segs = []
    for idx, alt in finals:
        best = refined.get(idx, alt)
        text = (best.get("text") or alt.get("text") or "").strip()
        if not text:
            continue
        start, end = int(alt.get("startTimeMs", 0)) / 1000, int(alt.get("endTimeMs", 0)) / 1000
        # languages — распределение вероятностей по языкам (набор языков непостоянен), а не список найденных:
        # считать упоминания нельзя, у каждого языка их поровну. Вес блока — его длительность,
        # иначе короткая иноязычная вставка перевесит долгую речь.
        for lg in alt.get("languages") or []:
            code = lg.get("languageCode", "").split("-")[0]
            prob = lg.get("probability")  # ноль — это ноль: «or 1» дало бы нулевому языку вес целого
            langs[code] = langs.get(code, 0.0) + (float(prob) if prob is not None else 1.0) * max(end - start, 0.001)
        segs += split_long(text, start, end, best.get("words") or alt.get("words") or [])
    segs.sort(key=lambda s: s["start"])
    lang = src.split("-")[0] if src else (max(langs, key=langs.get) if langs else None)
    return segs, lang


def asr_yandex(audio: Path, key: str, src: str | None, model: str = "general",
               state: Path | None = None, source: dict | None = None, wait: bool = True,
               poll: int = 5, raw_path: Path | None = None) -> tuple[list[dict], str | None, bool] | None:
    """Отправить звук (или продолжить сохранённую операцию — без повторной оплаты),
    дождаться и забрать результат. None — результат не готов, а ждать не велено."""
    op_id, submitted = None, False
    if state and state.exists():
        saved = load_json(state) or {}
        fresh = time.time() - saved.get("created", 0) < STT_KEEP_HOURS * 3600
        if (saved.get("model"), saved.get("src"), saved.get("source")) == (model, src or "auto", source) and fresh:
            op_id = saved["id"]
            log(f"      продолжаю операцию {op_id} (повторной оплаты нет)")
        else:
            log("      сохранённая операция устарела или не подходит — отправляю звук заново")
    if not op_id:
        op_id, submitted = stt_submit(audio, key, src, model), True
        if state:
            save_json(state, {"id": op_id, "model": model, "src": src or "auto", "source": source,
                              "created": time.time()})
        log(f"      операция {op_id} запущена ({'отложенный' if model.startswith('deferred') else 'стандартный'} режим)")
    t0 = time.time()
    while not stt_ready(op_id, key):
        if not wait:
            return None
        log(f"\r      ожидание {time.time() - t0:6.0f} с", end="")
        time.sleep(poll)
    log()
    segs, lang = stt_result(op_id, key, src, raw_path)
    return segs, lang, submitted


def transcribe(wav: Path, model_name: str | None, device: str, language: str | None,
               batch: int) -> tuple[list[dict], str]:
    """Локальное распознавание faster-whisper (--asr whisper)."""
    from faster_whisper import WhisperModel

    model_name = model_name or ("large-v3-turbo" if device == "cuda" else "small")
    compute = "float16" if device == "cuda" else "int8"
    log(f"      Whisper: модель {model_name}, {device}/{compute}")
    model = WhisperModel(model_name, device=device, compute_type=compute)
    if batch > 0:
        from faster_whisper import BatchedInferencePipeline
        segments, info = BatchedInferencePipeline(model).transcribe(
            str(wav), language=language, batch_size=batch, without_timestamps=False)
    else:
        segments, info = model.transcribe(str(wav), language=language, beam_size=5,
                                          vad_filter=True, condition_on_previous_text=False)
    result = [{"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip()}
              for s in segments if s.text.strip()]
    return result, info.language


def merge_phrases(segs: list[dict], max_len: float = MAX_SEG, max_gap: float = 0.8) -> list[dict]:
    out: list[dict] = []
    for s in segs:
        if out:
            p = out[-1]
            finished = p["text"].rstrip("\"'»”) ").endswith(SENTENCE_END)
            if not finished and s["start"] - p["end"] <= max_gap and s["end"] - p["start"] <= max_len:
                p["end"] = s["end"]
                p["text"] = f'{p["text"]} {s["text"]}'
                continue
        out.append(dict(s))
    return out


# ─────────────────────────── 3. перевод ───────────────────────────
def read_glossary(path: Path | None) -> list[list[str]]:
    """Строки «термин<TAB>перевод» или «термин = перевод»; # — комментарий; не более 50 пар."""
    if not path:
        return []
    if not path.is_file():
        sys.exit(f"Глоссарий не найден: {path}")
    pairs = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sep = "\t" if "\t" in line else " = "
        if sep not in line:
            sys.exit(f"Глоссарий: не разобрана строка «{line}» (нужно «термин<TAB>перевод» или «термин = перевод»)")
        pairs.append([x.strip() for x in line.split(sep, 1)])
    if len(pairs) > 50:
        sys.exit(f"Глоссарий: {len(pairs)} пар, лимит Yandex Translate — 50.")
    return pairs


def translate_yandex(texts: list[str], src: str | None, dst: str, key: str,
                     glossary: list[list[str]]) -> list[str]:
    body = {"targetLanguageCode": dst, "texts": texts, "format": "PLAIN_TEXT"}
    if src:
        body["sourceLanguageCode"] = src
    if glossary:
        body["glossaryConfig"] = {"glossaryData": {"glossaryPairs": [
            {"sourceText": s, "translatedText": t} for s, t in glossary]}}
    resp = json.loads(yc_request("POST", TRANSLATE_URL, key, body=body))
    out = [t.get("text", "") for t in resp.get("translations", [])]
    if len(out) != len(texts):
        raise RuntimeError(f"Translate вернул {len(out)} переводов на {len(texts)} фраз.")
    COST["tr_chars"] += sum(len(t) for t in texts)
    return out


def batches_by_chars(texts: list[str], limit: int = TR_BATCH_CHARS) -> list[list[str]]:
    out, cur, size = [], [], 0
    for t in texts:
        if cur and size + len(t) > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += len(t)
    return out + ([cur] if cur else [])


# ─────────────────────────── 4. озвучка ───────────────────────────
def tts_yandex(text: str, key: str, voice: str, role: str | None, speed: float,
               max_ms: int | None = None) -> np.ndarray:
    hints: list[dict] = [{"voice": voice}]
    if role:
        hints.append({"role": role})
    if abs(speed - 1.0) > 1e-6:
        hints.append({"speed": f"{speed:.2f}"})
    body = {"text": text, "hints": hints, "loudnessNormalizationType": "LUFS",
            "outputAudioSpec": {"rawAudio": {"audioEncoding": "LINEAR16_PCM",
                                             "sampleRateHertz": str(SR)}}}
    if len(text) > TTS_LIMIT:
        body["unsafeMode"] = True  # длинный текст: делится сервисом, тарифицируется по 250 символов
    elif max_ms:
        hints.append({"duration": {"policy": "MAX_DURATION", "durationMs": str(int(max_ms))}})
    raw = yc_request("POST", TTS_URL, key, body=body).decode("utf-8")
    chunks = []
    for obj in iter_json(raw):
        res = obj.get("result", obj)
        if "error" in res:
            raise RuntimeError(f"SpeechKit TTS: {res['error']}")
        if res.get("audioChunk", {}).get("data"):
            chunks.append(base64.b64decode(res["audioChunk"]["data"]))
    COST["tts_units"] += max(1, -(-len(text) // TTS_LIMIT))
    return np.frombuffer(b"".join(chunks), dtype="<i2")


def tts_name(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:16] + ".pcm"


def synth_all(jobs: list[tuple[str, Path, int | None]], key: str, voice: str, role: str | None,
              speed: float, workers: int = 4) -> None:
    """Синтез недостающих файлов кэша (параллельно; квота SpeechKit — 40 запросов/с)."""
    todo = [j for j in jobs if not j[1].exists()]
    done = 0

    def one(job):
        text, path, max_ms = job
        pcm = tts_yandex(text, key, voice, role, speed, max_ms)
        tmp = path.with_suffix(".part")
        pcm.astype("<i2").tofile(tmp)
        tmp.replace(path)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(one, todo):
            done += 1
            log(f"\r      {done}/{len(todo)}", end="")
    if todo:
        log()


# ─────────────────────────── 5. сборка ───────────────────────────
def build_voice_track(items: list[dict], pcms: list[np.ndarray], total: float, max_speed: float,
                      use_atempo: bool) -> tuple[np.ndarray, dict]:
    """Фраза стартует в своё время или сразу после предыдущей; при нехватке места —
    ускорение atempo (если разрешено) до max_speed, иначе сдвиг следующей фразы."""
    track = np.zeros(int((total + 0.5) * SR), dtype=np.int16)
    cursor = 0.0
    stats = {"sped": 0, "max_k": 1.0, "late": 0, "overflow": 0.0}
    for i, (item, pcm) in enumerate(zip(items, pcms)):
        if pcm.size == 0:
            continue
        start = max(item["start"], cursor)
        next_start = items[i + 1]["start"] if i + 1 < len(items) else total
        window = max(next_start - start, 0.2)
        k = min(max(pcm.size / SR / window, 1.0), max_speed)
        if use_atempo and k > 1.02:
            pcm = speed_up(pcm, k)
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
        src_lang: str | None, dst_lang: str) -> None:
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
        cmd += ["-metadata:s:a:1", f"language={ISO639_2.get(src_lang or '', 'und')}",
                "-metadata:s:a:1", "title=Оригинал", "-metadata:s:a:1", "handler_name=Оригинал",
                "-disposition:a:1", "0"]
    if ext in (".mp4", ".m4v", ".mov"):
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


def get_key() -> str:
    key = os.environ.get("YC_API_KEY", "").strip()
    if not key:
        sys.exit("Не задан ключ: переменная окружения YC_API_KEY (API-ключ сервисного аккаунта).\n"
                 "Windows: setx YC_API_KEY \"<ключ>\" и откройте новое окно терминала.")
    return key


def check_tools(need_opus: bool = False) -> None:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        sys.exit("Не найден ffmpeg/ffprobe в PATH. Windows: winget install Gyan.FFmpeg")
    if need_opus and "libopus" not in run(["ffmpeg", "-hide_banner", "-encoders"]).decode("utf-8", "replace"):
        sys.exit("ffmpeg собран без кодека Opus (libopus) — он нужен для отправки звука в SpeechKit. "
                 "Установите полную сборку: winget install Gyan.FFmpeg")
    import importlib.util
    if importlib.util.find_spec("requests") is None:
        sys.exit("Не установлен пакет requests: pip install -U requests numpy")


def self_check(voice: str) -> None:
    """Контрольная точка после регистрации: все три сервиса по кругу —
    перевод, синтез, распознавание синтезированной фразы."""
    check_tools(need_opus=True)
    key = get_key()
    log("1/3 Перевод (Yandex Translate)…")
    tr = translate_yandex(["Hello, world!"], "en", "ru", key, [])
    log(f"  OK: «Hello, world!» → «{tr[0]}»")
    log("2/3 Синтез (SpeechKit)…")
    phrase = "Проверка связи: один, два, три."
    pcm = tts_yandex(phrase, key, voice, None, 1.0)
    if pcm.size == 0:
        raise RuntimeError("SpeechKit вернул пустой звук.")
    log(f"  OK: {pcm.size / SR:.2f} с звука, голос {voice}")
    log("3/3 Распознавание (SpeechKit, асинхронно, обычно до минуты)…")
    with tempfile.TemporaryDirectory() as tmp:
        wav, ogg = Path(tmp) / "check.wav", Path(tmp) / "check.ogg"
        write_wav(wav, pcm)
        run(["ffmpeg", "-y", "-v", "error", "-i", str(wav), "-ac", "1", "-ar", "16000",
             "-c:a", "libopus", "-b:a", "32k", str(ogg)])
        segs, lang, _ = asr_yandex(ogg, key, "ru")
    COST["stt_sec"] = pcm.size / SR
    heard = " ".join(s["text"] for s in segs).strip()
    if not heard:
        raise RuntimeError("SpeechKit не распознал тестовую фразу.")
    log(f"  OK: распознано «{heard}» (отправлено «{phrase}»)")
    log(f"Всё готово: доступ к Translate и SpeechKit есть. Стоимость проверки: {cost_report('yandex')}")


def cost_report(asr: str, deferred: bool = False) -> str:
    price = PRICE_STT_DEFERRED_15S if deferred else PRICE_STT_15S
    stt = (max(15.0, COST["stt_sec"]) / 15 * price) if (asr == "yandex" and COST["stt_sec"]) else 0
    tr = COST["tr_chars"] / 1e6 * PRICE_TR_1M
    tts = COST["tts_units"] * PRICE_TTS_UNIT
    return (f"≈{stt + tr + tts:.2f} ₽ (распознавание {stt:.2f}, перевод {tr:.2f} "
            f"[{COST['tr_chars']} симв.], синтез {tts:.2f} [{COST['tts_units']} ед.])")


# ─────────────────────────── главный сценарий ───────────────────────────
def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Закадровый перевод видео через сервисы Яндекса.")
    ap.add_argument("input", type=Path, nargs="?", help="исходный видеофайл")
    ap.add_argument("--check", action="store_true", help="проверить ключ и доступ к сервисам и выйти")
    ap.add_argument("-o", "--output", type=Path, help="результат (по умолчанию <имя>_<язык>.mp4|.mkv)")
    ap.add_argument("--src", help="язык оригинала: en, de… или en-US (по умолчанию — автоопределение)")
    ap.add_argument("--dst", default="ru", help="язык перевода (по умолчанию ru)")
    ap.add_argument("--asr", default="yandex", choices=["yandex", "whisper"],
                    help="распознавание: yandex — SpeechKit; whisper — локально (pip install faster-whisper)")
    ap.add_argument("--fast", action="store_true",
                    help="стандартное распознавание SpeechKit: обычно минуты, но ≈в 4 раза дороже "
                         "(по умолчанию — отложенный режим: отправить звук и выйти, результат до 24 ч)")
    ap.add_argument("--wait", action="store_true",
                    help="в отложенном режиме не выходить, а ждать результата (проверка раз в минуту)")
    ap.add_argument("--model", help="модель Whisper (только --asr whisper)")
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda"], help="для --asr whisper")
    ap.add_argument("--batch", type=int, default=0, help="для --asr whisper: пакетный режим, напр. 8")
    ap.add_argument("--voice", default="alexander",
                    help="голос SpeechKit: alexander, kirill, anton, filipp (М); marina, jane, dasha, lera (Ж)…")
    ap.add_argument("--role", help="амплуа голоса: neutral, good, friendly, strict… (зависит от голоса)")
    ap.add_argument("--speed", type=float, default=1.0, help="темп озвучки, 0.5…2 (по умолчанию 1.0)")
    ap.add_argument("--fit", default="speechkit", choices=["speechkit", "atempo"],
                    help="подгонка длинных фраз: speechkit — пересинтез с лимитом длительности "
                         "(естественнее, +1 запрос); atempo — бесплатное ускорение ffmpeg")
    ap.add_argument("--glossary", type=Path, help="глоссарий: «термин<TAB>перевод» построчно, до 50 пар")
    ap.add_argument("--orig-volume", type=float, default=0.25, help="громкость оригинала, 0…1")
    ap.add_argument("--max-speed", type=float, default=1.5, help="предельное ускорение фразы")
    ap.add_argument("--no-original-track", action="store_true", help="без оригинальной дорожки")
    ap.add_argument("--workdir", type=Path, help="папка кэша (по умолчанию <имя>_work)")
    args = ap.parse_args(argv)

    if args.check:
        return self_check(args.voice)
    if not args.input:
        ap.error("укажите видеофайл или --check")
    if not 0.0 <= args.orig_volume <= 1.0 or not 1.0 <= args.max_speed <= 3.0 \
            or not 0.5 <= args.speed <= 2.0:
        sys.exit("Параметры вне диапазона: --orig-volume 0…1, --max-speed 1…3, --speed 0.5…2")
    check_tools(need_opus=args.asr == "yandex")
    key = get_key()
    glossary = read_glossary(args.glossary)
    if glossary and not args.src:
        log("Глоссарий требует явного языка оригинала — возьму язык, определённый распознаванием.")

    t0 = time.time()
    src_file = args.input.resolve()
    if not src_file.is_file():
        sys.exit(f"Файл не найден: {src_file}")
    out = (args.output or default_output(src_file, args.dst)).resolve()
    if out == src_file:
        sys.exit("Выходной файл совпадает с исходным — укажите другое имя в -o.")
    duration, has_audio, ours = probe(src_file)
    if not has_audio:
        sys.exit("В файле нет звуковой дорожки — переводить нечего.")
    if ours:  # не ошибка: пакетный запуск по *.mp4 должен идти дальше
        log(f"Пропуск: {src_file.name} — уже результат перевода (есть дорожка «Перевод»).")
        raise SystemExit(0)
    work = (args.workdir or src_file.with_name(src_file.stem + "_work")).resolve()
    (work / "tts").mkdir(parents=True, exist_ok=True)

    # 1–2. Распознавание (кэш: transcript.json)
    tr_path = work / "transcript.json"
    ckey = {"asr": args.asr, "src": args.src or "auto", "v": 2}  # v2: язык по вероятностям, а не по упоминаниям
    if args.asr == "whisper":
        ckey.update(model=args.model or "auto", device=args.device)
    cached = load_json(tr_path)
    if cached and cached.get("key") == ckey:
        segs, lang = cached["segments"], cached["language"]
        log(f"[1-2/5] Распознавание: из кэша (сегментов: {len(segs)}, язык: {lang})")
    else:
        if cached:
            log("      кэш прежней версии не подходит: в нём язык оригинала определён неверно")
        if args.asr == "yandex":
            audio, state = work / "audio.ogg", work / "stt_operation.json"
            stat = src_file.stat()
            source = {"size": stat.st_size, "mtime": int(stat.st_mtime)}
            if not (audio.exists() and state.exists()):
                log("[1/5] Извлечение звука…")
                run(["ffmpeg", "-y", "-v", "error", "-i", str(src_file), "-map", "0:a:0", "-vn", "-ac", "1",
                     "-ar", "16000", "-c:a", "libopus", "-b:a", "32k", str(audio)])
            model = "general" if args.fast else "deferred-general"
            log("[2/5] Распознавание SpeechKit…")
            res = asr_yandex(audio, key, args.src, model, state, source,
                             wait=args.fast or args.wait, poll=5 if args.fast else 60,
                             raw_path=work / "stt_raw.txt")
            if res is None:
                COST["stt_sec"] = duration if (load_json(state) or {}).get("created", 0) > t0 else 0.0
                cmd = " ".join(f'"{a}"' if " " in a else a for a in (argv if argv is not None else sys.argv[1:]))
                log("\nОтложенное распознавание ещё не готово (обычно от минут до 24 ч; результат хранится 3 суток).")
                if COST["stt_sec"]:
                    log(f"Оценка стоимости распознавания: {cost_report('yandex', True)}")
                log(f"Запустите ту же команду позже — программа продолжит с этого места:\n"
                    f"  python {Path(sys.argv[0]).name} {cmd}")
                raise SystemExit(0)
            segs, lang, submitted = res
            if submitted:
                COST["stt_sec"] = duration
        else:
            log("[1/5] Извлечение звука…")
            audio = work / "audio16k.wav"
            run(["ffmpeg", "-y", "-v", "error", "-i", str(src_file), "-map", "0:a:0", "-vn", "-ac", "1",
                 "-ar", "16000", str(audio)])
            log("[2/5] Распознавание Whisper (локально)…")
            segs, lang = transcribe(audio, args.model, args.device, args.src, args.batch)
        save_json(tr_path, {"key": ckey, "language": lang, "segments": segs})
        (work / "stt_operation.json").unlink(missing_ok=True)
    if not segs:
        sys.exit("Речь не обнаружена.")
    if lang == args.dst and not args.src:
        sys.exit(f"Язык видео определён как «{lang}» — перевод не нужен. Если распознавание ошиблось, "
                 f"назовите язык оригинала явно, например --src en: распознавание уже в кэше, "
                 f"повторной оплаты не будет.")
    src_tr = (args.src or lang or "").split("-")[0] or None
    if glossary and not src_tr:
        sys.exit("Для глоссария укажите язык оригинала: --src en")
    duration = duration or segs[-1]["end"] + 1.0

    # 3. Перевод (кэш по пачкам + ручная правка translation.json)
    phrases = merge_phrases(segs)
    texts = [p["text"] for p in phrases]
    sig = hashlib.sha1(json.dumps(["yandex-translate-v2", src_tr, glossary], ensure_ascii=False)
                       .encode("utf-8")).hexdigest()[:10]
    tl_path = work / "translation.json"
    cached = load_json(tl_path)
    if (cached and cached.get("dst") == args.dst and cached.get("sig") == sig
            and [x["src"] for x in cached["items"]] == texts):
        items = cached["items"]
        log(f"[3/5] Перевод: из кэша (фраз: {len(items)}, ручные правки сохранены)")
    else:
        memo_path = work / f"mt_cache_{args.dst}_{sig}.json"
        memo = load_json(memo_path) or {}
        need = list(dict.fromkeys(t for t in texts if t not in memo))
        log(f"[3/5] Перевод Yandex Translate: новых фраз {len(need)} из {len(texts)} "
            f"({src_tr or 'авто'} → {args.dst})…")
        done = 0
        for batch in batches_by_chars(need):
            memo.update(zip(batch, translate_yandex(batch, src_tr, args.dst, key, glossary)))
            save_json(memo_path, memo)
            done += len(batch)
            log(f"\r      {done}/{len(need)}", end="")
        log()
        items = [{"start": p["start"], "end": p["end"], "src": p["text"], "dst": memo[p["text"]]}
                 for p in phrases]
        if tl_path.exists():
            shutil.copyfile(tl_path, work / "translation.prev.json")
        save_json(tl_path, {"dst": args.dst, "sig": sig, "items": items})

    # 4. Озвучка (кэш: tts/<хэш>.pcm)
    speak = [it for it in items if re.search(r"\w", it["dst"])]
    base = (args.voice, args.role, args.speed)
    files = [work / "tts" / tts_name(*base, None, it["dst"]) for it in speak]
    log(f"[4/5] Озвучка SpeechKit: новых фраз {sum(not f.exists() for f in files)} из {len(speak)} "
        f"(голос {args.voice})…")
    synth_all([(it["dst"], f, None) for it, f in zip(speak, files)], key, *base)
    pcms = [trim_silence(np.fromfile(f, dtype="<i2")) for f in files]
    refit = 0
    if args.fit == "speechkit":  # пересинтез фраз, не помещающихся до следующей реплики
        jobs, fit_log = [], []
        for i, (it, pcm) in enumerate(zip(speak, pcms)):
            nxt = speak[i + 1]["start"] if i + 1 < len(speak) else duration
            window, dur = max(nxt - it["start"], 0.3), pcm.size / SR
            if dur > window * 1.02 and len(it["dst"]) <= TTS_LIMIT:
                max_ms = int(max(window, dur / args.max_speed) * 1000)
                jobs.append((it["dst"], work / "tts" / tts_name(*base, max_ms, it["dst"]), max_ms))
                fit_log.append({"phrase": i, "start_s": round(it["start"], 3), "chars": len(it["dst"]),
                                "window_ms": round(window * 1000), "requested_ms": max_ms,
                                "before_trimmed_ms": round(dur * 1000)})
        if jobs:
            log(f"      подгонка длительности: пересинтез {len(jobs)} фраз…")
            synth_all(jobs, key, *base)
            for rec, (_, f, _) in zip(fit_log, jobs):  # сколько звука вернул сервис на лимит MAX_DURATION
                pcm = np.fromfile(f, dtype="<i2")
                pcms[rec["phrase"]] = trim_silence(pcm)
                rec["after_ms"] = round(pcm.size / SR * 1000)
                rec["after_trimmed_ms"] = round(pcms[rec["phrase"]].size / SR * 1000)
            refit = len(jobs)
        # пишется и при пустом списке: «подгонка не потребовалась» отличимо от «механизм не сработал»
        save_json(work / "tts_fit.json", {"max_speed": args.max_speed, "phrases": fit_log})

    # 5. Сборка
    log("[5/5] Сведение дорожек и сборка файла…")
    track, stats = build_voice_track(speak, pcms, duration, args.max_speed, args.fit == "atempo")
    voice_wav = work / "voice.wav"
    write_wav(voice_wav, track)
    mux(src_file, voice_wav, out, args.orig_volume, not args.no_original_track, src_tr, args.dst)
    srt = out.with_suffix(".srt")
    write_srt(srt, items)

    log(f"\nГотово за {time.time() - t0:.0f} с")
    log(f"  видео:     {out}")
    log(f"  субтитры:  {srt}")
    log(f"  правка:    {tl_path}")
    log(f"  фраз: {len(speak)}; пересинтезировано под тайминг: {refit}; ускорено atempo: "
        f"{stats['sped']}; начато позже оригинала >0,5 с: {stats['late']}")
    log(f"  стоимость этого запуска (оценка по тарифам): {cost_report(args.asr, not args.fast)}")
    if stats["overflow"] > 0:
        log(f"  ВНИМАНИЕ: озвучка длиннее видео на {stats['overflow']:.1f} с — хвост обрезан.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nПрервано. Повторный запуск продолжит с места остановки (кэш в папке *_work).")
    except RuntimeError as exc:
        sys.exit(f"\nОШИБКА: {exc}")
