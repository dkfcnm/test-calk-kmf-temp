# API Яндекса: проверенные факты

Проверено по официальной документации **11.09.2026**. Раздел «Реальные ответы» заполнен по прогону
пользователя 11.09.2026 (60 с видео, `--fast`, `language_code: auto`). При расхождении с реальным
ответом прав реальный ответ — обнови этот файл и эмуляцию в `tests/test_yandex.py`.

## Аутентификация

| Факт | Значение | Источник |
|---|---|---|
| Заголовок | `Authorization: Api-Key <ключ>` (ключ сервисного аккаунта) | [Translate: аутентификация](https://aistudio.yandex.ru/en/docs/translate/api-ref/authentication) |
| ID каталога | Для сервисного аккаунта не передаётся (`folderId` в Translate, `x-folder-id` в SpeechKit) | там же; [TTS REST](https://aistudio.yandex.ru/docs/ru/speechkit/tts/api/tts-v3-rest.html) |
| Получение ключа | AI Studio → «Создать API-ключ»: сервисный аккаунт с ролью `ai.editor` создаётся сам; области ключа включают распознавание, синтез и перевод | [Получить API-ключ](https://aistudio.yandex.ru/ru/docs/ai-studio/operations/get-api-key) |
| Хранение | Только переменная окружения `YC_API_KEY`; в файлы и логи не пишется (код маскирует ключ в ошибках) | решение проекта |

## Translate v2

| Факт | Значение | Источник |
|---|---|---|
| Запрос | `POST https://translate.api.cloud.yandex.net/translate/v2/translate`; поля `texts[]`, `targetLanguageCode`, `sourceLanguageCode`, `format`, `glossaryConfig.glossaryData.glossaryPairs[]` | [Translation.Translate](https://aistudio.yandex.ru/en/docs/translate/api-ref/Translation/translate) |
| Лимиты | Суммарно ≤ 10 000 символов в `texts[]` (код режет пачки по 9 000); глоссарий 1–50 пар; при глоссарии `sourceLanguageCode` обязателен | там же |
| Ответ | `translations[{text, detectedLanguageCode}]` — порядок как у входа | там же |
| Тариф | 500,4 ₽ за 1 млн символов с НДС; отложенного/пакетного режима нет | [Тарифы Translate](https://aistudio.yandex.ru/docs/ru/translate/pricing.html) |

## SpeechKit: синтез (TTS v3 REST)

| Факт | Значение | Источник |
|---|---|---|
| Запрос | `POST https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis`; `hints[]` — **в каждом элементе одно поле**: `voice`, `role`, `speed`, `duration{policy, durationMs}` и др.; `outputAudioSpec.rawAudio{audioEncoding: LINEAR16_PCM, sampleRateHertz}`; `unsafeMode` | [UtteranceSynthesis](https://aistudio.yandex.ru/ru/docs/speechkit/tts-v3/api-ref/Synthesizer/utteranceSynthesis) |
| Подгонка длительности | `duration.policy = MAX_DURATION` ограничивает длину звука — так длинные фразы укладываются в тайминг | там же |
| Ответ | Поток JSON-объектов подряд (не массив), звук в `result.audioChunk.data` (base64); числа int64 — строками | там же; пример в [TTS REST](https://aistudio.yandex.ru/docs/ru/speechkit/tts/api/tts-v3-rest.html) |
| Лимиты | 250 символов и 24 с на запрос; в `unsafeMode` — до 5 000 символов; 40 запросов/с | [Квоты SpeechKit](https://aistudio.yandex.ru/ru/docs/speechkit/concepts/limits) |
| Тариф | 0,1626 ₽ за запрос с НДС; в `unsafeMode` — за каждые 250 символов | [Тарифы SpeechKit](https://aistudio.yandex.ru/ru/docs/speechkit/pricing) |
| Голоса ru-RU | М: alexander, kirill, anton, ermil, zahar, filipp; Ж: marina (по умолчанию сервиса), dasha, julia, lera, masha, jane, omazh; амплуа зависят от голоса. `alena` в текущем списке нет | [Список голосов](https://aistudio.yandex.ru/ru/docs/speechkit/tts/voices) |

## SpeechKit: распознавание (STT v3 REST, асинхронно)

| Факт | Значение | Источник |
|---|---|---|
| Отправка | `POST https://stt.api.cloud.yandex.net/stt/v3/recognizeFileAsync`; `content` (base64) **или** `uri` (Object Storage); `recognition_model{model, audio_format, text_normalization, language_restriction}` | [RecognizeFile](https://yandex.cloud/ru-kz/docs/speechkit/stt-v3/api-ref/AsyncRecognizer/recognizeFile) |
| Модели | `general` — стандартный режим; `deferred-general` — отложенный (низкий приоритет, свой тариф) | [Асинхронное распознавание](https://aistudio.yandex.ru/ru/docs/speechkit/stt/transcribation) |
| Статус | `GET https://operation.api.cloud.yandex.net/operations/{id}` → `done` | [Пример API v3](https://aistudio.yandex.ru/ru/docs/speechkit/stt/api/transcribation-api-v3) |
| Результат | `GET https://stt.api.cloud.yandex.net/stt/v3/getRecognition?operation_id=…` → поток объектов `result`: `final` (сырой текст, слова с таймкодами), `finalRefinement.normalizedText` (нормализованный), `eouUpdate` | [GetRecognition](https://yandex.cloud/ru-kz/docs/speechkit/stt-v3/api-ref/AsyncRecognizer/getRecognition) |
| Язык | `language_code: ["auto"]` — определение по каждому предложению | [Языки распознавания](https://aistudio.yandex.ru/docs/ru/speechkit/stt/models.html) |
| Сроки | От минут до 24 ч в обоих режимах; результат хранится 3 суток; «≈10 с на минуту звука» — ориентир для стандартного | [Асинхронное распознавание](https://aistudio.yandex.ru/ru/docs/speechkit/stt/transcribation) |
| Лимиты | В теле запроса ≤ 60 МБ (код шлёт OGG Opus 32 кбит/с ≈ 14 МБ/ч); звук ≤ 4 ч; 500 запросов на распознавание в час; статус v3 — 5 запросов/с | [Квоты SpeechKit](https://aistudio.yandex.ru/ru/docs/speechkit/concepts/limits) |
| Тариф | Стандартный 0,1515 ₽, отложенный 0,0381 ₽ за 15 с с НДС; посекундно с 16-й секунды; моно считается как 2 канала. ⚠ Оценка в коде множитель за каналы не применяет: 60 с в стандартном режиме дали 0,61 ₽ = 60 / 15 × 0,1515. Фактическое списание со счётом не сверялось | [Тарифы SpeechKit](https://aistudio.yandex.ru/ru/docs/speechkit/pricing) |

## Регистрация (резидент РФ)

Грант не менее 4 000 ₽ на 60 дней, один раз, только если карта российского банка привязана **при
создании** платёжного аккаунта. После окончания гранта нужен ручной переход на платную версию,
иначе доступ приостанавливается. Источник: исходники документации Yandex Cloud на GitHub
(`ru/getting-started/usage-grant.md`, `ru/_includes/billing/bonus-account.md`).

## Реальные ответы SpeechKit

Источник: прогон 11.09.2026, 60 с речи, модель `general`, `language_code: ["auto"]`,
`literature_text: true`. Дословный ответ сохраняется в `<имя>_work/stt_raw.txt`, эмуляция —
`recognition_real()` в `tests/test_yandex.py`.

| Факт | Наблюдение | Последствие для кода |
|---|---|---|
| Нарезка `final` | Блоками по ~30 с (0–30 660 мс и 30 660–60 000 мс), а не по фразам | Резать самим: `split_long` по паузам между словами, порог `MAX_SEG` |
| Пунктуация | **Отсутствует**: сплошной текст строчными буквами, без точек и запятых, несмотря на `literature_text: true` | `merge_phrases` не видит конца предложения (`SENTENCE_END`) и склеивает соседние сегменты всегда, пока зазор ≤ 0,8 с и длина ≤ 12 с. Перевод получает текст без пунктуации |
| `finalRefinement.normalizedText` | Совпал с `final` дословно, включая таймкоды слов | Выбор «нормализованный, иначе сырой» безопасен, но выигрыша в этом ответе не дал |
| `languages` при `auto` | Не список найденных языков, а **распределение вероятностей по всем языкам модели** (13 записей в каждом `final`): ru-RU 0,751; kk-KK 0,221; en-EN 0,025; остальные < 0,01 | Язык выбирается по сумме `probability`, а не по числу упоминаний: у всех языков упоминаний поровну |
| Вставки другого языка | Английские реплики внутри русской речи попадают в тот же `final` латиницей («we call a fan in and a fu yin so»); отдельного языка для них нет | Смешанная речь переводится целиком как один язык |
| Дополнительные поля | `sessionUuid{uuid,userRequestId}`, `responseWallTimeMs`, `audioCursors{receivedDataMs,resetTimeMs,partialTimeMs,finalTimeMs,finalIndex,eouTimeMs}`, `confidence: 0`, `channelTag` | Не используются; разбор устойчив к их наличию |

## Реальное поведение MAX_DURATION

| Параметр | Значение |
|---|---|
| Фраза | 49 символов, естественная длительность 2555 мс |
| Запрошенный лимит | 2360 мс (равен окну до следующей реплики) |
| Вернул сервис | 2352 мс — уложился в лимит, речь заметно не рвётся |
| После обрезки тишины | 2021 мс |

Сильное сжатие так и не проверено: лимит считается как `max(окно, длительность / --max-speed)`,
поэтому при широком окне он не опускается ниже окна, и `--max-speed 3` ничего не меняет. Чтобы
получить жёсткий случай, нужна фраза, чьё окно до следующей реплики само по себе мало.

## Тупики (не повторять)

| Что | Почему отказались |
|---|---|
| Загрузка звука в Object Storage | Не нужна: до 60 МБ звук передаётся прямо в запросе |
| Синтез через API v1 | Примерно на треть дешевле при фразах ~80 символов (оценка), но без подгонки длительности и с меньшим выбором голосов |
| Голос `alena` | Отсутствует в текущем списке голосов |
| Отложенный режим для перевода/синтеза | Существует только у распознавания |
