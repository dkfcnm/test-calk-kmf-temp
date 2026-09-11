"""Тесты yandex_video_translate.py без сети: локальный сервер отвечает в форматах документации
Yandex Cloud (поток склеенных JSON-объектов для SpeechKit). Ожидаемые значения — ручной расчёт."""
import base64, http.server, json, os, subprocess, sys, threading
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import numpy as np

import tempfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yandex_video_translate as yv
from media import make_media

HERE = Path(tempfile.mkdtemp(prefix="vt_test_"))  # медиа и артефакты — во временной папке
make_media(HERE)
print("Рабочая папка теста:", HERE)
KEY = "test-key-yc"
REQS, SCRIPT = [], []          # журнал запросов; заготовленные «сбойные» ответы
TR_MAP = {"A.": "Альфа.", "B.": "Б" * 29 + ".", "C.": "В" * 39 + ".", "D.": "Гамма-дел."}
OP_POLLS = {"n": 0}
DEFER = {"ready": False, "polls": 0, "ready_after": None}


def tone(sec: float) -> bytes:
    t = np.arange(int(sec * 24000)) / 24000
    return (np.sin(2 * np.pi * 1000 * t) * 0.3 * 32767).astype("<i2").tobytes()


def pretty(*objs) -> str:  # как в примере документации: объекты подряд, без разделителей
    return "".join(json.dumps(o, ensure_ascii=False, indent=1) for o in objs)


def recognition() -> str:
    parts = []
    for i, (s, e, raw, norm) in enumerate([(1000, 3000, "a", "A."), (5000, 6000, "b", "B."),
                                           (7000, 8000, "c", "C."), (9000, 10000, "d", "D.")]):
        alt = {"words": [{"text": raw, "startTimeMs": str(s), "endTimeMs": str(e)}], "text": raw,
               "startTimeMs": str(s), "endTimeMs": str(e), "languages": [{"languageCode": "en-US", "probability": "0.99"}]}
        cur = {"finalIndex": str(i)}
        parts.append({"result": {"audioCursors": cur, "final": {"alternatives": [alt], "channelTag": "0"}}})
        parts.append({"result": {"audioCursors": cur, "finalRefinement": {"finalIndex": str(i), "normalizedText": {
            "alternatives": [dict(alt, text=norm)], "channelTag": "0"}}}})
        parts.append({"result": {"eouUpdate": {"timeMs": str(e)}}})
    return pretty(*parts)


class Handler(http.server.BaseHTTPRequestHandler):
    def reply(self, status, payload):
        data = (payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)).encode()
        self.send_response(status); self.send_header("content-length", str(len(data))); self.end_headers()
        self.wfile.write(data)

    def handle_any(self, body):
        u = urlparse(self.path)
        REQS.append((self.command, u.path, dict(self.headers), body, parse_qs(u.query)))
        if self.headers.get("Authorization") != f"Api-Key {KEY}":
            return self.reply(401, {"message": f"Unknown api key '{self.headers.get('Authorization')}'"})
        if SCRIPT:
            return self.reply(*SCRIPT.pop(0))
        if u.path == "/translate/v2/translate":
            return self.reply(200, {"translations": [{"text": TR_MAP.get(t, "RU:" + t), "detectedLanguageCode": "en"}
                                                     for t in body["texts"]]})
        if u.path == "/tts/v3/utteranceSynthesis":
            dur = 0.1 * len(body["text"])
            for h in body["hints"]:
                if "duration" in h and h["duration"]["policy"] == "MAX_DURATION":
                    dur = min(dur, int(h["duration"]["durationMs"]) / 1000)
            pcm = tone(dur); half = len(pcm) // 4 * 2
            chunks = [{"result": {"audioChunk": {"data": base64.b64encode(p).decode()}}} for p in (pcm[:half], pcm[half:])]
            return self.reply(200, pretty(*chunks))
        if u.path == "/stt/v3/recognizeFileAsync":
            deferred = body["recognition_model"]["model"] == "deferred-general"
            return self.reply(200, {"id": "op-def" if deferred else "op-1", "done": False})
        if u.path == "/operations/op-def":
            DEFER["polls"] += 1
            done = DEFER["ready"] or (DEFER["ready_after"] is not None and DEFER["polls"] >= DEFER["ready_after"])
            return self.reply(200, {"id": "op-def", "done": done})
        if u.path == "/operations/op-1":
            OP_POLLS["n"] += 1
            return self.reply(200, {"id": "op-1", "done": OP_POLLS["n"] >= 2})
        if u.path == "/stt/v3/getRecognition":
            return self.reply(200, recognition())
        return self.reply(404, {"message": "no route"})

    def do_POST(self):
        self.handle_any(json.loads(self.rfile.read(int(self.headers["content-length"]))))

    def do_GET(self):
        self.handle_any(None)

    def log_message(self, *a):
        pass


_real_main = yv.main
def _guarded_main(argv):
    try:
        return _real_main(argv)
    except SystemExit as e:
        if argv and "--check" not in argv and e.code == 0 and "def_work" not in " ".join(argv):
            check(f"неожиданный выход с кодом 0: {argv}", False)
        raise
yv.main = _guarded_main
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}"
yv.TRANSLATE_URL, yv.TTS_URL = BASE + "/translate/v2/translate", BASE + "/tts/v3/utteranceSynthesis"
yv.STT_URL, yv.GETREC_URL = BASE + "/stt/v3/recognizeFileAsync", BASE + "/stt/v3/getRecognition"
yv.OPERATION_URL = BASE + "/operations"
yv.time.sleep = lambda s: None  # паузы опроса и повторов не ждём
os.environ["YC_API_KEY"] = KEY
os.environ.pop("YC_FOLDER_ID", None)

ok = True
def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)

def amp(x, f, t1, t2, sr=24000):
    seg = x[int(t1 * sr):int(t2 * sr)]
    t = np.arange(seg.size) / sr
    return 2 * abs(np.sum(seg * np.exp(-2j * np.pi * f * t))) / max(seg.size, 1)

def track(path, idx):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", f"0:a:{idx}", "-ac", "1", "-ar", "24000",
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)

def streams(path):
    return json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
                                     capture_output=True, check=True).stdout)["streams"]

def calls(path):
    return [r for r in REQS if r[1] == path]

print("1. Разбор и вспомогательные функции")
check("iter_json: склеенные и построчные объекты", list(yv.iter_json('{"a":1}\n{"b":2}{"c":3}')) == [{"a": 1}, {"b": 2}, {"c": 3}])
check("iter_json: JSON-массив", list(yv.iter_json('[{"a":1},{"b":2}]')) == [{"a": 1}, {"b": 2}])
w = [{"text": f"w{i}", "startTimeMs": str(i * 1000), "endTimeMs": str(i * 1000 + 800)} for i in range(20)]
s = yv.split_long("t", 0, 20, w)
check("split_long без пауз: жёсткий разрез ≥12 с → 0–12,8 (13 слов) и 13–19,8 (7)",
      [(x["start"], x["end"], len(x["text"].split())) for x in s] == [(0.0, 12.8, 13), (13.0, 19.8, 7)], s)
w2 = [dict(x, startTimeMs=str(int(x["startTimeMs"]) + (500 if i > 10 else 0)),
           endTimeMs=str(int(x["endTimeMs"]) + (500 if i > 10 else 0))) for i, x in enumerate(w)]
s2 = yv.split_long("t", 0, 20.3, w2)
check("split_long с паузой 0,7 с после 10,8 с → 0–10,8 (11) и 11,5–20,3 (9)",
      [(x["start"], x["end"], len(x["text"].split())) for x in s2] == [(0.0, 10.8, 11), (11.5, 20.3, 9)], s2)
check("split_long: короткое высказывание не режется", yv.split_long("abc", 1, 5, w) == [{"start": 1, "end": 5, "text": "abc"}])
check("batches_by_chars", yv.batches_by_chars(["aaaa", "bbbb", "cc"], 8) == [["aaaa", "bbbb"], ["cc"]])
g = HERE / "g.tsv"
g.write_text("\ufeff# термины\n\nDay Master\tГосподин дня\nTen Gods = Десять божеств\n", encoding="utf-8")
check("глоссарий: BOM, комментарий, TAB и « = »", yv.read_glossary(g) == [["Day Master", "Господин дня"], ["Ten Gods", "Десять божеств"]])
g.write_text("\n".join(f"t{i}\tп{i}" for i in range(51)), encoding="utf-8")
try:
    yv.read_glossary(g); check("51 пара должна отклоняться", False)
except SystemExit as e:
    check("глоссарий >50 пар отклонён", "50" in str(e.code), e.code)

print("2. Контрольная точка --check")
REQS.clear(); OP_POLLS["n"] = 0
yv.COST.update(stt_sec=0.0, tr_chars=0, tts_units=0)
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    yv.main(["--check"])
out_check = buf.getvalue()
check("--check: перевод + синтез + распознавание, заголовок Api-Key, без folderId",
      len(calls("/translate/v2/translate")) == 1 and len(calls("/tts/v3/utteranceSynthesis")) == 1
      and len(calls("/stt/v3/recognizeFileAsync")) == 1 and len(calls("/stt/v3/getRecognition")) == 1
      and "folderId" not in calls("/translate/v2/translate")[0][3])
stt_c = calls("/stt/v3/recognizeFileAsync")[0][3]
check("--check: синтезированная фраза отправлена на распознавание в OGG, язык ru-RU",
      base64.b64decode(stt_c["content"])[:4] == b"OggS"
      and stt_c["recognition_model"]["language_restriction"]["language_code"] == ["ru-RU"]
      and stt_c["recognition_model"]["model"] == "general")
check("--check: отчёт — три OK и стоимость (15 с распознавания — минимум тарифа)",
      out_check.count("  OK:") == 3 and "распознавание 0.15" in out_check, out_check.strip().splitlines()[-1])

print("3. Полный прогон: SpeechKit → Translate → SpeechKit")
REQS.clear(); OP_POLLS["n"] = 0
yv.COST.update(stt_sec=0.0, tr_chars=0, tts_units=0)
src = HERE / "тест видео.mp4"
work = HERE / "ya_work"
gl = HERE / "terms.tsv"; gl.write_text("B.\tБета.\n", encoding="utf-8")
out = HERE / "ya.mp4"
yv.main([str(src), "--fast", "--workdir", str(work), "-o", str(out), "--glossary", str(gl)])
stt = calls("/stt/v3/recognizeFileAsync")[0][3]
check("STT: звук OGG в content, модель general, язык auto, нормализация и литературный стиль",
      base64.b64decode(stt["content"])[:4] == b"OggS" and stt["recognition_model"]["model"] == "general"
      and stt["recognition_model"]["language_restriction"]["language_code"] == ["auto"]
      and stt["recognition_model"]["text_normalization"]["literature_text"] is True)
check("STT: опрос операции до done и запрос результата по operation_id",
      len(calls("/operations/op-1")) == 2 and calls("/stt/v3/getRecognition")[0][4] == {"operation_id": ["op-1"]})
tr = calls("/translate/v2/translate")[0][3]
check("Translate: нормализованный текст (с точкой), язык из распознавания, глоссарий, без folderId",
      tr["texts"] == ["A.", "B.", "C.", "D."] and tr["sourceLanguageCode"] == "en" and "folderId" not in tr
      and tr["glossaryConfig"]["glossaryData"]["glossaryPairs"] == [{"sourceText": "B.", "translatedText": "Бета."}], tr)
tts = calls("/tts/v3/utteranceSynthesis")
dur_hints = sorted(h["duration"]["durationMs"] for _, _, _, b, _ in tts for h in b["hints"] if "duration" in h)
check("TTS: 4 синтеза + пересинтез 2 фраз с MAX_DURATION 2000 и 2666 мс (ручной расчёт)",
      len(tts) == 6 and dur_hints == ["2000", "2666"], dur_hints)
check("TTS: голос alexander, PCM 24 кГц, без unsafeMode для коротких фраз",
      tts[0][3]["hints"][0] == {"voice": "alexander"} and tts[0][3]["outputAudioSpec"]["rawAudio"]
      == {"audioEncoding": "LINEAR16_PCM", "sampleRateHertz": "24000"} and "unsafeMode" not in tts[0][3])
raw_stt = (work / "stt_raw.txt").read_bytes()  # байтами: текстовое чтение скрыло бы порчу концов строк
check("сырой ответ распознавания сохранён дословно, байт в байт", raw_stt == recognition().encode("utf-8"),
      len(raw_stt))
fit = json.loads((work / "tts_fit.json").read_text(encoding="utf-8"))
ph = fit["phrases"]
check("отчёт о подгонке: 2 фразы, окно 2000 мс, лимиты 2000 и 2666 мс (ручной расчёт), звук уложен в лимит",
      fit["max_speed"] == 1.5
      and [(f["phrase"], f["chars"], f["window_ms"], f["requested_ms"], f["before_trimmed_ms"]) for f in ph]
      == [(1, 30, 2000, 2000, 3000), (2, 40, 2000, 2666, 4000)]
      and all(0 < f["after_trimmed_ms"] <= f["after_ms"] <= f["requested_ms"] for f in ph), ph)
st = streams(out)
a = [x for x in st if x["codec_type"] == "audio"]
check("выход: h264 без перекодирования + 2 дорожки rus/eng", st[0]["codec_name"] == "h264" and len(a) == 2
      and [x["tags"].get("language") for x in a] == ["rus", "eng"])
t0 = track(out, 0)
on = [amp(t0, 1000, *iv) for iv in [(1.05, 1.55), (5.05, 6.95), (7.10, 9.65), (9.82, 10.70)]]
off = [amp(t0, 1000, *iv) for iv in [(1.75, 4.9), (11.0, 19.5)]]
check("озвучка в расчётных интервалах (A 1,0–1,6; B 5,0–7,0; C 7,05–9,72; D 9,77–10,77)", min(on) > 0.1, [round(v, 3) for v in on])
check("в паузах тишина", max(off) < 0.005, [round(v, 4) for v in off])
srt = out.with_suffix(".srt").read_text(encoding="utf-8")
check("SRT: 4 блока, первый «Альфа.» 00:00:01,000", srt.startswith("1\n00:00:01,000 --> 00:00:03,000\nАльфа.\n") and srt.count("-->") == 4)
rep = yv.cost_report("yandex")
check("оценка стоимости: 20 с распознавания, 8 символов, 6 единиц синтеза",
      "распознавание 0.20" in rep and "[8 симв.]" in rep and "[6 ед.]" in rep and "синтез 0.98" in rep, rep)
leaks = [p.name for p in work.rglob("*") if p.is_file() and KEY.encode() in p.read_bytes()]
check("ключ не записан ни в один файл", not leaks, leaks)

print("4. Кэш: повторный запуск без запросов")
REQS.clear(); yv.main([str(src), "--fast", "--workdir", str(work), "-o", str(out), "--glossary", str(gl)])
check("повтор: 0 запросов к API", len(REQS) == 0, [r[1] for r in REQS])

print("5. Сбои и ошибки")
REQS.clear(); SCRIPT[:] = [(500, {"message": "internal"})]
res = yv.translate_yandex(["X."], "en", "ru", KEY, [])
check("HTTP 500 → повтор → успех", res == ["RU:X."] and len(REQS) == 2)
os.environ["YC_API_KEY"] = "wrong-key"
try:
    yv.translate_yandex(["X."], "en", "ru", "wrong-key", []); check("401 должен остановить", False)
except yv.YCAuthError as e:
    check("HTTP 401 → понятная ошибка, ключ скрыт", "wrong-key" not in str(e) and "YC_API_KEY" in str(e), str(e)[:120])
os.environ["YC_API_KEY"] = KEY
os.environ.pop("YC_API_KEY")
try:
    yv.main(["--check"]); check("без ключа должен быть выход", False)
except SystemExit as e:
    check("нет YC_API_KEY → подсказка setx", "setx" in str(e.code))
os.environ["YC_API_KEY"] = KEY

print("6. Вариант --asr whisper и --fit atempo")
yv.transcribe = lambda wav, m, d, l, b: ([{"start": 1.0, "end": 3.0, "text": "A."}, {"start": 5.0, "end": 6.0, "text": "C."}], "en")
REQS.clear()
yv.main([str(src), "--asr", "whisper", "--fit", "atempo", "--workdir", str(HERE / "wh_work"), "-o", str(HERE / "wh.mkv")])
check("whisper: без запросов к SpeechKit STT, перевод и синтез через Яндекс",
      not calls("/stt/v3/recognizeFileAsync") and len(calls("/translate/v2/translate")) == 1
      and len(calls("/tts/v3/utteranceSynthesis")) == 2 and (HERE / "wh.mkv").exists())

print("7. Отложенное распознавание и возобновление операции")
import io, contextlib
STT, GETREC = "/stt/v3/recognizeFileAsync", "/stt/v3/getRecognition"
dw = HERE / "def_work"; argv = [str(src), "--workdir", str(dw), "-o", str(HERE / "def.mp4")]
DEFER["ready"] = False; REQS.clear(); yv.COST.update(stt_sec=0.0, tr_chars=0, tts_units=0)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    try:
        yv.main(argv); code = "нет выхода"
    except SystemExit as e:
        code = e.code
out1 = buf.getvalue()
state = json.loads((dw / "stt_operation.json").read_text(encoding="utf-8"))
check("1-й запуск без флагов: отложенный режим (deferred-general), выход с кодом 0, операция сохранена, команда подсказана",
      code == 0 and calls(STT)[0][3]["recognition_model"]["model"] == "deferred-general"
      and state["id"] == "op-def" and state["model"] == "deferred-general" and '"' + str(src) + '"' in out1
      and not calls(GETREC), code)
check("1-й запуск: стоимость по отложенному тарифу — 20 с × 0,0381 ₽/15 с ≈ 0,05 ₽", "распознавание 0.05" in out1,
      [l for l in out1.splitlines() if "стоимост" in l.lower()])
DEFER["ready"] = True; REQS.clear()
yv.main(argv)
check("2-й запуск: без повторной отправки звука, результат забран, видео собрано, файл операции удалён",
      not calls(STT) and len(calls(GETREC)) == 1 and (HERE / "def.mp4").exists()
      and not (dw / "stt_operation.json").exists())
(dw / "transcript.json").unlink()
stat = src.stat()
json.dump({"id": "op-1", "model": "general", "src": "auto", "source": {"size": stat.st_size, "mtime": int(stat.st_mtime)},
           "created": yv.time.time()}, open(dw / "stt_operation.json", "w", encoding="utf-8"))
REQS.clear(); yv.main([str(src), "--fast", "--workdir", str(dw), "-o", str(HERE / "def.mp4")])
check("прерванный стандартный запуск: операция подхвачена без повторной оплаты",
      not calls(STT) and len(calls(GETREC)) == 1)
(dw / "transcript.json").unlink()
json.dump({"id": "op-1", "model": "general", "src": "auto", "source": {"size": stat.st_size, "mtime": int(stat.st_mtime)},
           "created": yv.time.time() - 4 * 86400}, open(dw / "stt_operation.json", "w", encoding="utf-8"))
REQS.clear(); yv.main([str(src), "--fast", "--workdir", str(dw), "-o", str(HERE / "def.mp4")])
check("операция старше 3 суток: звук отправлен заново", len(calls(STT)) == 1)
with contextlib.redirect_stdout(io.StringIO()):
    yv.main(["-h"]) if False else None
help_txt = subprocess.run([sys.executable, str(ROOT / "yandex_video_translate.py"), "-h"], capture_output=True, text=True).stdout
check("справка: есть --fast и --wait, нет --deferred", "--fast" in help_txt and "--wait" in help_txt and "--deferred" not in help_txt)

print("8. --wait в отложенном режиме и защита от повторной обработки")
DEFER.update(ready=False, polls=0, ready_after=3); REQS.clear(); yv.COST.update(stt_sec=0.0, tr_chars=0, tts_units=0)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    yv.main([str(src), "--wait", "--workdir", str(HERE / "wait_work"), "-o", str(HERE / "wait.mp4")])
outw = buf.getvalue()
check("--wait: отложенный режим, ожидание до готовности (3 опроса), видео собрано, стоимость по отложенному тарифу",
      calls(STT)[0][3]["recognition_model"]["model"] == "deferred-general" and len(calls("/operations/op-def")) == 3
      and (HERE / "wait.mp4").exists() and "распознавание 0.05" in outw, [l for l in outw.splitlines() if "стоимость" in l])
check("--wait: сырой ответ и отчёт о подгонке сохранены и в отложенном режиме",
      (HERE / "wait_work" / "stt_raw.txt").read_bytes() == recognition().encode("utf-8")
      and json.loads((HERE / "wait_work" / "tts_fit.json").read_text(encoding="utf-8"))["phrases"])
for f in (HERE / "ya.mp4", HERE / "wh.mkv"):
    REQS.clear(); buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            _real_main([str(f)]); code = "нет выхода"
        except SystemExit as e:
            code = e.code
    check(f"{f.name}: выходной файл распознан по дорожке «Перевод» — пропуск с кодом 0, без запросов к API",
          code == 0 and "Пропуск" in buf.getvalue() and not REQS, (code, buf.getvalue().strip()))

print("\nИТОГ:", "ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ" if ok else "ЕСТЬ ОШИБКИ")
srv.shutdown()
sys.exit(0 if ok else 1)
