"""Смоук-тесты video_translate.py (v1: Google + Edge) без сети: ASR, перевод и TTS заменены заглушками.
Ожидаемые значения — литералы, рассчитанные вручную (не тем же кодом)."""
import json, subprocess, sys
from pathlib import Path
import numpy as np

import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import video_translate as vt
from media import make_media

HERE = Path(tempfile.mkdtemp(prefix="vt1_test_"))  # медиа и артефакты — во временной папке
make_media(HERE)
print("Рабочая папка теста:", HERE)
calls = {"asr": 0, "tr": 0, "tts": 0}
TTS_DUR = {"А-перевод.": 2.0, "Б-перевод.": 3.0, "В-перевод.": 4.0, "Г-перевод.": 1.0, "Г-исправлено.": 1.0}

SEGS = [  # старт, конец, текст — сценарий из ручного расчёта
    {"start": 1.0, "end": 3.0, "text": "A."},
    {"start": 5.0, "end": 6.0, "text": "B."},
    {"start": 7.0, "end": 8.0, "text": "C."},
    {"start": 9.0, "end": 10.0, "text": "D."},
]

def fake_transcribe(wav, model, device, lang, batch):
    calls["asr"] += 1
    assert Path(wav).exists(), "не извлечён звук"
    return [dict(s) for s in SEGS], "en"

def fake_translate(texts, src, dst, workers=4):
    calls["tr"] += 1
    m = {"A.": "А-перевод.", "B.": "Б-перевод.", "C.": "В-перевод.", "D.": "Г-перевод."}
    return [m[t] for t in texts]

async def fake_synth(jobs, voice, rate, parallel=4):
    for text, path in jobs:
        calls["tts"] += 1
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"sine=frequency=1000:sample_rate=24000:duration={TTS_DUR.get(text, 1.0)}",
                        "-ac", "1", "-c:a", "libmp3lame", "-b:a", "48k", "-f", "mp3", str(path)],
                       check=True)

vt.transcribe, vt.translate_texts, vt.synthesize = fake_transcribe, fake_translate, fake_synth

def amp(x, f, t1, t2, sr=24000):
    seg = x[int(t1 * sr):int(t2 * sr)]
    t = np.arange(seg.size) / sr
    return 2 * abs(np.sum(seg * np.exp(-2j * np.pi * f * t))) / max(seg.size, 1)

def track(path, idx):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", f"0:a:{idx}", "-ac", "1",
                          "-ar", "24000", "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)

def streams(path):
    return json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
                                      str(path)], capture_output=True, check=True).stdout)

ok = True
def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))

# ── 1. Чистые функции ──
print("1. Чистые функции")
check("srt_time(3661.5)", vt.srt_time(3661.5) == "01:01:01,500", vt.srt_time(3661.5))
check("srt_time(0.0004)", vt.srt_time(0.0004) == "00:00:00,000")
check("atempo(3.0)", vt.atempo(3.0) == "atempo=2.0,atempo=1.5000", vt.atempo(3.0))
m = vt.merge_phrases([{"start": 0, "end": 1, "text": "Hello"}, {"start": 1.2, "end": 2, "text": "world."},
                      {"start": 2.1, "end": 3, "text": "Next one"}, {"start": 5, "end": 6, "text": "far away."}])
check("merge_phrases", [(p["start"], p["end"], p["text"]) for p in m] ==
      [(0, 2, "Hello world."), (2.1, 3, "Next one"), (5, 6, "far away.")], m)
x = np.concatenate([np.zeros(2400, np.int16), np.full(4800, 5000, np.int16), np.zeros(2400, np.int16)])
y = vt.trim_silence(x)
check("trim_silence: 0,2 с звука (4800) + 2×0,04 с запаса (1920) = 6720 отсчётов", y.size == 4800 + 2 * 960, y.size)
tone = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=500:r=24000:d=3", "-f", "s16le",
                       "-ac", "1", "-"], capture_output=True, check=True).stdout
check("to_pcm tempo 1.5: 3,0 с → 2,0 с", abs(vt.to_pcm(tone, 1.5).size / 24000 - 2.0) < 0.03,
      vt.to_pcm(tone, 1.5).size / 24000)

# ── 2. Полный прогон mp4 (кириллица и пробел в имени) ──
print("2. Полный прогон: «тест видео.mp4»")
src = HERE / "тест видео.mp4"
vt.main([str(src)])
out = HERE / "тест видео_ru.mp4"
info = streams(out)
st = info["streams"]
v = [s for s in st if s["codec_type"] == "video"]
a = [s for s in st if s["codec_type"] == "audio"]
check("выход существует, 1 видео (h264, без перекодирования) + 2 аудио",
      len(v) == 1 and v[0]["codec_name"] == "h264" and len(a) == 2, [s["codec_name"] for s in st])
check("метаданные дорожек", [s["tags"].get("language") for s in a] == ["rus", "eng"]
      and [s["tags"].get("handler_name") for s in a] == ["Перевод", "Оригинал"]
      and [s["disposition"]["default"] for s in a] == [1, 0],
      [(s["tags"], s["disposition"]["default"]) for s in a])
check("длительность ≈ 20 с", abs(float(info["format"]["duration"]) - 20) < 0.2, info["format"]["duration"])
t0, t1 = track(out, 0), track(out, 1)
spans_on = [(1.10, 2.90), (5.10, 6.90), (7.15, 9.60), (9.87, 10.66)]
spans_off = [(3.15, 4.90), (11.0, 19.5)]
on = [amp(t0, 1000, *s) for s in spans_on]
off = [amp(t0, 1000, *s) for s in spans_off]
check("озвучка звучит в расчётных интервалах", min(on) > 0.05, [round(v, 3) for v in on])
check("в паузах озвучки нет", max(off) < 0.005, [round(v, 4) for v in off])
ratio = amp(t0, 440, 11, 19) / amp(t1, 440, 11, 19)
check("оригинал приглушён до 0,25", abs(ratio - 0.25) < 0.02, round(ratio, 4))
check("в дорожке «Оригинал» озвучки нет", amp(t1, 1000, 1.1, 2.9) < 0.005)
srt = out.with_suffix(".srt").read_text(encoding="utf-8")
expected_srt = ("1\n00:00:01,000 --> 00:00:03,000\nА-перевод.\n\n2\n00:00:05,000 --> 00:00:06,000\nБ-перевод.\n\n"
                "3\n00:00:07,000 --> 00:00:08,000\nВ-перевод.\n\n4\n00:00:09,000 --> 00:00:10,000\nГ-перевод.\n")
check("SRT совпадает с эталоном", srt == expected_srt, repr(srt[:80]))
check("вызовы: ASR 1, перевод 1, TTS 4", calls == {"asr": 1, "tr": 1, "tts": 4}, calls)

# ── 3. Статистика размещения по ручному расчёту ──
print("3. Размещение и ускорение")
work = HERE / "тест видео_work"
items = json.loads((work / "translation.json").read_text(encoding="utf-8"))["items"]
files = [work / "tts" / vt.tts_name("ru-RU-DmitryNeural", "+10%", it["dst"]) for it in items]
_, stats = vt.build_voice_track(items, files, 20.0, 1.5)
check("ускорено 2 фразы, макс ×1,5, опоздание >0,5 с — 1", stats["sped"] == 2 and abs(stats["max_k"] - 1.5) < 1e-9
      and stats["late"] == 1 and stats["overflow"] == 0, stats)

# ── 4. Повторный запуск и ручная правка ──
print("4. Кэш и правка перевода")
calls.update(asr=0, tr=0, tts=0)
vt.main([str(src)])
check("повтор: ASR 0, перевод 0, TTS 0", calls == {"asr": 0, "tr": 0, "tts": 0}, calls)
data = json.loads((work / "translation.json").read_text(encoding="utf-8"))
data["items"][3]["dst"] = "Г-исправлено."
(work / "translation.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
vt.main([str(src)])
check("после правки: переозвучена 1 фраза", calls == {"asr": 0, "tr": 0, "tts": 1}, calls)
check("правка попала в SRT", "Г-исправлено." in out.with_suffix(".srt").read_text(encoding="utf-8"))

# ── 5. Контейнеры и граничные случаи ──
print("5. Контейнеры и граничные случаи")
vt.main([str(HERE / "surround.mkv")])
o = HERE / "surround_ru.mkv"
a = [s for s in streams(o)["streams"] if s["codec_type"] == "audio"]
check("5.1 в mkv: собрано, 2 аудио, названия дорожек", o.exists() and len(a) == 2
      and [s["tags"].get("title") for s in a] == ["Перевод", "Оригинал"], [(s["codec_name"], s["channels"]) for s in a])
check("5.1: озвучка есть в дорожке перевода", amp(track(o, 0), 1000, 1.1, 2.9) > 0.02)
vt.main([str(HERE / "clip.webm")])
o = HERE / "clip_ru.mkv"
check("webm → mkv, VP9 скопирован", o.exists() and streams(o)["streams"][0]["codec_name"] == "vp9")
vt.main([str(src), "--no-original-track", "-o", str(HERE / "single.mp4")])
check("--no-original-track: 1 аудио", sum(s["codec_type"] == "audio" for s in streams(HERE / "single.mp4")["streams"]) == 1)
for argv, needle in ([str(HERE / "noaudio.mp4")], "нет звуковой дорожки"), \
                    ([str(src), "--rate", "10%"], "--rate"), \
                    ([str(src), "-o", str(src)], "совпадает"), \
                    ([str(src), "--dst", "xx"], "--voice"):
    try:
        vt.main(argv); check(f"ожидался выход: {needle}", False)
    except SystemExit as e:
        check(f"понятная ошибка: {needle}", needle in str(e.code), e.code)

print("6. Обрыв сети посреди перевода и возобновление")
vt.TRANSLATE_CHUNK = 2
calls.update(asr=0, tr=0, tts=0)
real_tr = vt.translate_texts
def flaky(texts, src, dst, workers=4):
    if calls["tr"] == 1:
        calls["tr"] += 1
        raise RuntimeError("сеть недоступна")
    return fake_translate(texts, src, dst)
vt.translate_texts = flaky
try:
    vt.main([str(HERE / "clip.webm"), "--workdir", str(HERE / "resume_work"), "-o", str(HERE / "resume.mkv")])
    check("ожидалась ошибка на 2-й пачке", False)
except RuntimeError:
    memo = json.loads((HERE / "resume_work" / "mt_cache_ru.json").read_text(encoding="utf-8"))
    check("после обрыва сохранена 1-я пачка (2 фразы)", len(memo) == 2, memo)
calls.update(tr=0)
vt.translate_texts = fake_translate
vt.main([str(HERE / "clip.webm"), "--workdir", str(HERE / "resume_work"), "-o", str(HERE / "resume.mkv")])
check("повтор: переведена только 2-я пачка (1 вызов), файл собран", calls["tr"] == 1 and (HERE / "resume.mkv").exists(), calls)
vt.translate_texts, vt.TRANSLATE_CHUNK = real_tr, 50

print("\nИТОГ:", "ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ" if ok else "ЕСТЬ ОШИБКИ")
sys.exit(0 if ok else 1)
