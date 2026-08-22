# Telegram Keyword Monitor

Невеликий event-driven сервіс, який працює від імені вашого звичайного Telegram-акаунта:

1. слухає лише явно налаштовані канали та групи;
2. перевіряє текст нового повідомлення або caption медіа;
3. шукає ключові слова чи фрагменти слів;
4. за потреби класифікує повідомлення як `accept` або `reject` через AI;
5. надсилає повідомлення у Saved Messages або через окремого Telegram-бота, додаючи результат
   класифікації лише тоді, коли AI observation справді виконувався.

На відміну від періодичного опитування MCP, сервіс тримає одне підключення до Telegram і
отримує `NewMessage` одразу після появи повідомлення. Він читає тільки ті діалоги, до яких
використаний акаунт уже має доступ; права адміністратора не потрібні.

Детальний опис компонентів і потоків даних: [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Що вже реалізовано

- окремі правила для кожного каналу або чату;
- `notify_all = true` для каналів, де потрібен кожен новий пост;
- `skip_ai = true` для джерел, де потрібні keyword-фільтри й звичайна доставка без AI;
- Unicode NFKC + case-insensitive substring matching (`"ваканс"` знайде `"вакансія"`);
- повідомлення коротші за 10 символів після вилучення `)` та emoji й обрізання пробілів
  автоматично пропускаються;
- повідомлення, текст яких після вилучення `)` та emoji й обрізання кінцевих пробілів
  закінчується на `?`, пропускаються;
- робота з текстом і підписами до фото/відео;
- підтримка public username, `t.me` URL і числових `-100…` ID приватних груп;
- посилання на оригінальне повідомлення, коли Telegram дозволяє його побудувати;
- асинхронна черга сповіщень, щоб повільна доставка не блокувала приймання updates;
- до п'яти спроб доставки з exponential backoff і підтримкою Bot API `retry_after`;
- bot-підписки через `/start` і `/stop`, персистентний ліміт до 10 користувачів;
- in-memory дедуплікація за `(chat_id, message_id)`;
- bounded startup buffer, який приймає updates ще до завершення завантаження діалогів;
- автоматичне прибирання дубля, коли пост watched-каналу форвардиться у watched discussion;
- автоматичне перепідключення та Telethon catch-up після тимчасового розриву з'єднання;
- disabled-by-default типізована конфігурація AI observation, один поточний prompt bundle,
  бінарний контракт `accept`/`reject` та ізольований асинхронний OpenAI Responses client
  з bounded retries і нормалізованими технічними результатами;
- observation інтегрований після всіх детермінованих фільтрів для keyword-matched
  повідомлень: `accept` доставляє alert, `reject` пропускає Telegram delivery і записує весь
  сформований alert як `WARNING`, а технічна помилка працює fail-open; `notify_all` і per-source
  `skip_ai = true` повністю обходять AI та використовують звичайний delivery path; один
  logical observation не повторюється через Telegram delivery retries;
- окрема opt-in команда `ai-check --live` для одноразового OpenAI smoke test
  без запуску Telegram, notifier-а чи subscriber database;
- повністю офлайн unit та integration-style тести.

## Швидкий старт

Потрібен Python 3.11+ і Telegram API credentials із
[my.telegram.org/apps](https://my.telegram.org/apps).

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
cp config.example.toml config.toml
```

Обов'язкову приватну базову policy зберігайте в кореневому `policy-prompt.txt`. Винесені
розширені нормативні приклади можна опціонально зберігати поруч у
`policy-prompt-extended-examples.txt`: loader додає їх після базової policy. Відсутній або
порожній extension-файл означає роботу лише з базовою policy. Як і `config.toml`, обидва
prompt-файли ігноруються Git. Базова policy потрібна для ввімкненого AI observation або
ручного `ai-check --live`; обидва файли, коли вони використовуються, мають бути у UTF-8.

Для міграції на split policy:

1. Перемістіть із `policy-prompt.txt` **сам заголовок**
   `РОЗШИРЕНІ НОРМАТИВНІ ПРИКЛАДИ` і весь текст нижче нього в
   `policy-prompt-extended-examples.txt`.
2. Видаліть цей блок із `policy-prompt.txt`, щоб приклади не застосовувалися двічі й base
   prompt справді став коротшим.
3. На Railway повторіть той самий split: `AI_POLICY_PROMPT` має містити base без цього
   блока, а `AI_POLICY_PROMPT_EXTENDED_EXAMPLES` — заголовок і весь перенесений хвіст.
   Перевірте, що кожна variable окремо вкладається в Railway limit.

Заповніть у `.env` `TELEGRAM_API_ID` та `TELEGRAM_API_HASH`, після чого створіть окрему
user-session. `OPENAI_API_KEY` потрібен, коли `[ai_observation].enabled = true` або коли
ви явно запускаєте `ai-check --live`:

```bash
telegram-monitor generate-session
```

Команда попросить номер телефону, одноразовий код і, якщо ввімкнено, пароль 2FA. Скопіюйте
надрукований `TELEGRAM_SESSION_STRING` у `.env`.

> Session string має повний доступ вашого Telegram-клієнта. Не комітьте `.env`, не
> передавайте session string третім особам і не запускайте ту саму session одночасно в MCP
> та в monitor. Для monitor згенеруйте окрему session/auth key.

## Налаштування джерел і фільтрів

Спочатку подивіться доступні діалоги:

```bash
telegram-monitor list-chats
```

Команда показує ваш `YOUR_USER_ID`, тип діалогу, username, title і стабільний marked ID.
Відредагуйте локальний `config.toml`, створений із
[`config.example.toml`](config.example.toml):

```toml
notification_mode = "saved_messages"
notify_to = "me"
timezone = "Europe/Kyiv"

# Кожен новий пост каналу.
[[sources]]
peer = "@product_updates"
notify_all = true

# Лише цікаві повідомлення discussion-групи.
[[sources]]
peer = "@product_updates_chat"
keywords = ["kubernetes", "terraform", "ваканс", "знижк"]
keywords_to_skip = ["spam", "реклама", "casino"]
label = "Product discussion"

# Для приватної supergroup використовуйте ID з list-chats.
[[sources]]
peer = -1001234567890
keywords = ["incident", "реліз"]
keywords_to_skip = ["resolved"]
skip_ai = true
```

`config.toml` доданий до `.gitignore`, тому ваші Telegram IDs і правила не потраплять у Git.
За замовчуванням файл читається з поточної директорії. Інший шлях можна задати у `.env`,
наприклад `MONITOR_CONFIG_FILE=~/.config/telegram-monitor/config.toml`.

Discussion, прикріплена до каналу, є окремою supergroup. Додайте її як окреме джерело —
username або `-100…` ID самої discussion-групи, а не каналу.

Якщо і канал, і його discussion відстежуються одночасно, форвард поста каналу в discussion
за замовчуванням не створює другий alert. Щоб бачити обидві копії, встановіть
`skip_forwards_from_watched_sources = false` у `config.toml`.

Правило спочатку шукає **будь-який** фрагмент із `keywords` або пропускає цей етап для
`notify_all = true`. Після позитивної перевірки застосовується опціональний
`keywords_to_skip`: якщо знайдено хоча б один його фрагмент, повідомлення не створює alert.
Негативний фільтр має пріоритет і працює також разом із `notify_all = true`. Для обох списків
регістр не має значення, використовується Unicode substring match, а порожні фрагменти
відкидаються. Для перевірок довжини й останнього символу з тексту спочатку вилучаються всі
`)` та emoji. Після цього повідомлення коротше 10 символів після `strip()` пропускається;
рівно 10 символів уже проходить цю перевірку. Повідомлення, яке після `rstrip()`
закінчується на `?`, також завжди пропускається — навіть при збігу keyword або
`notify_all = true`. Оригінальний текст із `)` та emoji залишається для keyword-фільтрів і
alert. Правило без позитивних keywords дозволене лише з `notify_all = true`.

Per-source `skip_ai = true` не змінює жодного з цих детермінованих правил: позитивний збіг
із `keywords` усе одно обов'язковий, а `keywords_to_skip` так само має пріоритет. Лише після
успішної фільтрації такий queue job обходить observer/OpenAI, не отримує AI result block і
доставляється звичайним шляхом. Це відрізняється від `notify_all = true`, який також
пропускає вимогу позитивного keyword match. Значення `skip_ai` за замовчуванням — `false`.

Перевірити конфіг без Telegram-запитів:

```bash
telegram-monitor check "Шукаємо Kubernetes-інженера"
```

Повертається exit code `0`, якщо спрацювало хоча б одне правило, і `1`, якщо збігів немає.

## Конфігурація AI observation

AI observation вимкнений за замовчуванням і не змінює чинну детерміновану фільтрацію. Після
ввімкнення він виконує pre-delivery класифікацію лише для повідомлень, що пройшли keyword
path джерела з `skip_ai = false`. Повідомлення з `notify_all = true` або `skip_ai = true`
повністю обходять AI: OpenAI request не створюється, AI-блок до alert не додається, а
повідомлення йде звичайним delivery path. Різниця в тому, що `notify_all` пропускає вимогу
позитивного keyword match, тоді як `skip_ai` діє тільки після звичайних `keywords` і
`keywords_to_skip`. Для AI-eligible keyword path `accept` продовжує доставку, а `reject`
зупиняє її та записує сформований alert у warning log. Для
ввімкнення додайте окрему таблицю **після всіх top-level settings і до першої**
`[[sources]]` (це важливо через правила scoping у TOML):

```toml
[ai_observation]
enabled = false
model = "gpt-5.4-nano-2026-03-17"
prompt_bundle_path = "prompts"
policy_prompt_path = "policy-prompt.txt"
policy_prompt_extended_examples_path = "policy-prompt-extended-examples.txt"
default_trusted_area_context = "Львів"
operation_timeout_seconds = 30
request_attempts = 2
retry_base_seconds = 0.5
retry_max_seconds = 2.0
reasoning_effort = "medium"
max_output_tokens = 800
store_responses = false

[[sources]]
peer = "@example_discussion"
keywords = ["хмар", "зелен"]
skip_ai = false
trusted_area_context = "Львів та околиці"
```

Основні правила конфігурації:

- `enabled = false` зберігає поточну поведінку й не вимагає API key або наявності bundle;
- `operation_timeout_seconds` обмежений 30 секундами й задає верхню межу. Клієнт отримує від
  caller-а залишок спільного timeout budget, обмежує його цим значенням і ділить між усіма
  HTTP attempts, retry-паузами, розбором та перевіркою рішення; новий повний timeout для
  кожної спроби не запускається;
- під час shutdown worker отримує один повний AI budget і ще 5 секунд delivery grace, після
  чого решта черги скасовується best-effort, щоб зупинка не зависала безмежно;
- `request_attempts` задає **загальну** кількість HTTP attempts від 1 до 3, включно з першою
  спробою. Вбудовані retries OpenAI SDK вимкнені, щоб кількість запитів і timeout залишалися
  під контролем застосунку;
- `retry_base_seconds` і `retry_max_seconds` задають bounded exponential backoff. Retry
  виконується лише для тимчасових transport/server помилок і справжнього rate limiting;
- `reasoning_effort` може бути `none`, `low`, `medium`, `high` або `xhigh`;
- `store_responses = false` не зберігає response як application state для подальшого
  отримання через Responses API і є рекомендованим значенням для observation mode. Це не
  є гарантією Zero Data Retention: окремо діють налаштування та політики зберігання даних
  OpenAI, описані в [офіційному data controls guide](https://developers.openai.com/api/docs/guides/your-data);
- `trusted_area_context` конкретного source має перевагу над
  `default_trusted_area_context`; порожній рядок нормалізується до відсутнього контексту;
- `skip_ai = true` конкретного source обходить observer, OpenAI request і AI result block,
  але не змінює `keywords`, `keywords_to_skip`, deduplication, formatting або delivery;
- відносний `prompt_bundle_path` обчислюється від директорії самого `config.toml`, а не від
  поточної робочої директорії. Для `MONITOR_CONFIG_FILE` в іншому каталозі скопіюйте туди
  `prompts` або вкажіть абсолютний шлях;
- відносний `policy_prompt_path` так само обчислюється від директорії `config.toml`. За
  замовчуванням це приватний кореневий `policy-prompt.txt`, який є в `.gitignore`;
- відносний `policy_prompt_extended_examples_path` обчислюється за тим самим правилом. Це
  опціональний приватний файл: якщо він існує й непорожній, його вміст додається після
  базової policy; відсутній або порожній файл означає base-only режим;
- `prompts` містить поточні `system-prompt.txt` і `response-format.json`; private
  base policy завантажується окремо з файла або `AI_POLICY_PROMPT`, а optional extension —
  із другого файла або `AI_POLICY_PROMPT_EXTENDED_EXAMPLES`;
- `response-format.json` містить поточний strict JSON Schema wrapper з іменем
  `telegram_mobility_observation`, придатний для передачі в
  Responses API як `text.format`. Локальний loader перевіряє структуру schema, а parser —
  JSON-форму та обов'язкове бінарне рішення відповідно до
  [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
  а ізольований client використовує цей формат у фактичному request;
- `prompt_hash` — повний SHA-256 від system prompt, фактично сформованої приватної policy
  (base плюс завантажений optional extension) та канонічного JSON усього response format.
  Модель, API key, джерела policy, шляхи, timeout і retries до hash не входять. Hash є
  fingerprint-ом фактично завантаженого вмісту, а не номером його версії;
- `OPENAI_API_KEY` створюється в OpenAI Platform і зберігається лише в `.env`, як описано в
  [офіційному quickstart](https://developers.openai.com/api/docs/quickstart#create-and-export-an-api-key).
  Валідатор перевіряє його наявність без повернення, показу чи логування значення.

Ізольований `AsyncOpenAI` Responses client формує request з поточних prompt-ів і вхідних
даних, виконує контрольовані retries в одному timeout budget, перевіряє Structured Output та
повертає типізоване рішення або нормалізований технічний статус. `timeout`, `rate_limited`,
`refusal`, `api_error` та `invalid_response` не перетворюються на семантичний `reject` і
залишають доставку відкритою за fail-open правилом.

Важливі semantic mappings:

- цей контракт застосовується лише до keyword path із `skip_ai = false`; `notify_all` і
  `skip_ai = true` не створюють AI request або AI-рішення;
- `decision` має лише два значення: `accept` і `reject`;
- для звичайного keyword path модель спочатку шукає явний критерій відхилення з private
  policy. Якщо жоден критерій не спрацював, результат за замовчуванням — `accept`;
- відсутня або неоднозначна локація, подія чи час сама по собі не є причиною відхилення;
- `location` і `event` належать до результату `accept`; `reason_code` і короткий `reason` —
  до результату `reject`. Усі чотири поля є optional на рівні застосунку, і порушення цього
  розподілу не переводить результат в `invalid_response`;
- `reason_code` містить лише reject-причини, зокрема `spam_or_scam`,
  `unrelated_content`, `only_opinion_or_emotion` та `political_commentary`.

Strict Structured Outputs має окрему wire-вимогу: усі properties JSON object повинні бути
перелічені в `required`. Тому модель завжди повертає ключі `decision`, `location`, `event`,
`reason_code` і `reason`, а логічно optional поля мають nullable тип і передаються як `null`,
коли не стосуються рішення. Це технічне представлення не робить їх обов'язковими в
доменному контракті.

Коли observation увімкнений, notification worker викликає його для keyword path із
`skip_ai = false` лише після source/filter, deduplication та automatic-forward перевірок.
Уся AI-операція має один end-to-end budget до 30 секунд. `accept` формує та надсилає один
alert до Saved Messages або bot subscribers. `reject` формує той самий повний alert, але не
надсилає його в Telegram: однорядкова безпечна версія записується як structured `WARNING` у
`stdout`, тому Railway розпізнає `level = warn`. `notify_all` та `skip_ai = true` jobs одразу
переходять до звичайного formatter/delivery без OpenAI request і без `AI analysis:`.
Telegram retries повторюють вже сформований рядок і не створюють нового OpenAI request.
Технічні помилки AI-eligible keyword path залишаються fail-open: вихідне повідомлення
надсилається з технічним AI-блоком. Створення client-а не виконує live API probe; для цього
path setup failure нормалізується як fail-open `api_error`.

Alert для AI-eligible keyword path після семантичного рішення містить блок `AI analysis:` із
загальним `Delay` у секундах з трьома знаками після крапки, наприклад `Delay: 0.842 s`.
Для доставленого `accept` додаються наявні `Location` та `Event`, а redundant `Decision` не
показується. Для `reject`, який потрапляє лише в warning log, залишаються `Decision`, наявні
`Reason code` та `Reason`.
Усі auxiliary-поля optional і показуються лише за наявності. Model і token usage у Telegram
не показуються, але залишаються доступними як безпечні runtime metadata для application
logs. Технічний блок містить лише `Status` та однореченнєвий `Description`.

### Ручний AI smoke test

`ai-check` виконує одну логічну класифікацію через той самий Responses client,
prompt bundle, private policy, model і JSON Schema, що й production observation. Це
реальний OpenAI API request: текст передається OpenAI, виклик може бути
платним, а тимчасова помилка може призвести до `request_attempts` HTTP attempts.

Явний `--live` обов’язковий, щоб випадковий запуск не зробив мережевий запит:

```bash
telegram-monitor ai-check --live \
  --trusted-area-context "Львів" \
  --matched-keyword "хмар" \
  --matched-keyword "зелен" \
  --message-age-seconds 8 \
  "У Львові дуже хмарно, буде злива!"
```

або

```bash
.venv/bin/telegram-monitor ai-check --live \
  --trusted-area-context "Львів" \
  --matched-keyword "хмар" \
  --matched-keyword "зелен" \
  --message-age-seconds 8 \
  "У Львові дуже хмарно, буде злива!"
```

Замість positional text можна вказати `--stdin`, вставити повідомлення і
завершити input через `Ctrl+D`. Це не залишає повний текст у shell history чи
process arguments:

```bash
telegram-monitor ai-check --live --stdin \
  --trusted-area-context "Львів" \
  --matched-keyword "хмар"
```

Рівно одне джерело тексту є обов’язковим: positional argument або `--stdin`.
Доступні додаткові входи:

- `--trusted-area-context TEXT` — перевизначити глобальний trusted area для цього
  виклику;
- `--matched-keyword TEXT` — додати prefilter match; flag можна повторювати;
- `--message-age-seconds N` — задати невід’ємний вік повідомлення; за
  замовчуванням `0`.

Потрібно передати хоча б один `--matched-keyword`, щоб вхід відповідав production keyword
path. `ai-check` не має `--notify-all`: production-повідомлення з `notify_all = true`
взагалі не викликають AI, тому для них немає AI smoke-test path.

Якщо `[ai_observation].enabled = false`, `--live` одноразово активує лише manual
request у поточному процесі. Файл `config.toml` не змінюється, production observation
не вмикається. Для live request все одно потрібні чинні `OPENAI_API_KEY`,
prompt bundle і private policy. Команда не створює Telethon client, notifier чи
subscriber store, не надсилає Telegram alert і не змінює persistent state.

Успіх — це будь-яке валідне семантичне рішення: `accept` або `reject`.
Воно повертає exit code `0` і один JSON object у stdout:

```json
{
  "kind": "semantic",
  "decision": "accept",
  "location": "Липники, ліс у напрямку Львова",
  "event": "хмарно; згадані зелені",
  "metadata": {
    "model": "gpt-5.4-nano-2026-03-17",
    "prompt_hash": "...",
    "api_latency_seconds": 0.82,
    "attempts": 1,
    "token_usage": {
      "input_tokens": 1000,
      "output_tokens": 80,
      "total_tokens": 1080
    }
  }
}
```

Для `reject` CLI пропускає `location` і `event`, а `reason_code` і `reason` описують явний
критерій відхилення. Serializer додає auxiliary keys лише тоді, коли відповідне значення
наявне. Raw model wire format при цьому залишається strict і використовує `null` для
логічно optional properties.

Технічний стан не маскується під семантичне рішення; він повертає exit code `3`:

```json
{
  "kind": "technical_failure",
  "status": "rate_limited",
  "metadata": {
    "model": "gpt-5.4-nano-2026-03-17",
    "prompt_hash": "...",
    "api_latency_seconds": 1.2,
    "attempts": 2
  }
}
```

`api_latency_seconds` — тривалість усього ізольованого classification client cycle:
HTTP attempts, retries, backoff, parsing і перевірка `decision`. Це число в секундах; воно
не включає підготовку observation report, яка додатково враховується в `Delay` production
alert-а.

Exit code `2` означає невірні arguments або configuration/resources error, `130` —
переривання користувачем. Команда не додає до output API key, raw prompts, raw API
response, raw exception або окрему копію input envelope. Водночас нормалізовані поля
`location`, `event` і `reason` є вмістом відповіді моделі та, коли заповнені, можуть містити
фрагменти вхідного повідомлення. Токени в metadata є лише кількісною usage-статистикою.
Якщо OpenAI не повернув token usage, поле `token_usage` буде `null`.

### Приватна policy та optional extension на Railway

У Railway `AI_POLICY_PROMPT` є обов'язковою для ввімкненого AI observation: додайте її до
service і вставте базову policy з локального `policy-prompt.txt` як звичайний
багаторядковий текст. Якщо потрібна винесена секція розширених прикладів, додайте також
опціональну `AI_POLICY_PROMPT_EXTENDED_EXAMPLES` із вмістом локального
`policy-prompt-extended-examples.txt`. Loader додає цей extension після базової policy.

```text
AI_POLICY_PROMPT=<multiline base policy; required>
AI_POLICY_PROMPT_EXTENDED_EXAMPLES=<multiline extended examples; optional>
```

Не обгортайте значення в JSON, не замінюйте переноси рядків на текст `\n` і не кодуйте його
в Base64. Railway підтримує multiline variables: новий рядок можна вставити через
`Ctrl+Enter` / `Cmd+Enter` або Raw Editor. За потреби variable можна позначити як sealed.
[Railway documentation: multiline variables](https://docs.railway.com/variables#multiline-variables),
[sealed variables](https://docs.railway.com/variables#sealed-variables).
Після додавання або зміни variable перегляньте staged changes і виконайте deploy, інакше
running service не отримає нове значення.

Правила вибору базової policy:

1. Якщо `AI_POLICY_PROMPT` присутня, використовується тільки її значення.
2. Якщо variable відсутня, читається локальний `policy_prompt_path`.
3. Порожня `AI_POLICY_PROMPT` є помилкою і не маскується fallback-файлом.
4. Якщо немає ні variable, ні файла, loader повертає безпечну configuration error без
   виведення policy в лог.

Правила вибору optional extension:

1. Непорожня `AI_POLICY_PROMPT_EXTENDED_EXAMPLES` має пріоритет над локальним
   `policy_prompt_extended_examples_path`.
2. Присутня, але порожня variable явно вимикає extension; fallback до файла не виконується.
3. Якщо variable відсутня, loader читає configured extension-файл, лише коли він існує.
4. Відсутній або порожній extension-файл не є помилкою: request використовує тільки базову
   policy.

Таким чином Railway deployment потребує `AI_POLICY_PROMPT`, але
`AI_POLICY_PROMPT_EXTENDED_EXAMPLES` лишається опціональною; жоден приватний prompt не
потрібен у Git або Docker image. Після вставлення тих самих частин hash буде однаковим
локально й на Railway: loader нормалізує типи переносів рядків і завершальні newline перед
обчисленням fingerprint.

У runtime використовуються поточні значення цих variables. Доступ до них надавайте лише тим
людям і процесам, яким дозволено бачити policy; не виводьте environment dump у логи.

## Куди надсилати сповіщення

### Saved Messages — найпростіший варіант

Конфіг із `notification_mode = "saved_messages"` копіює результати в Telegram Saved Messages.
Це зручно як журнал, але Telegram може не показувати push для повідомлень, відправлених вашим
власним акаунтом.

### Telegram bot — справжній push

Щоб розсилати звичайні push-сповіщення підписникам:

1. створіть бота через `@BotFather`;
2. додайте `TELEGRAM_BOT_TOKEN` у `.env`;
3. змініть конфіг:

```toml
notification_mode = "bot"
bot_subscriber_limit = 10
bot_subscriber_database = ".state/bot_subscribers.sqlite3"
timezone = "Europe/Kyiv"
```

4. запустіть monitor;
5. кожен користувач, який хоче отримувати alerts, має відкрити бота й надіслати `/start`.

Сервер отримує ці команди через Bot API long polling. Перші 10 унікальних активних
користувачів зберігаються в SQLite і отримують кожен наступний alert. Повторний `/start`
ідемпотентний. `/stop` видаляє підписку та звільняє слот. Якщо всі 10 слотів зайняті, новий
користувач одразу отримає відповідь
`Максимальна кількість користувачів перевищена (ліміт: 10)`.

Команди приймаються лише в приватному чаті з ботом. Для одного token запускайте лише один
екземпляр monitor: Telegram не дозволяє одночасно використовувати `getUpdates` у кількох
процесах або разом з активним webhook. Якщо webhook налаштований, застосунок завершить старт
із поясненням і не видалятиме його автоматично.

У Docker через `restart: unless-stopped` така конфігураційна помилка повторюватиметься в
логах після кожного рестарту. Зупиніть сервіс, приберіть webhook або виправте token, а потім
запустіть його знову.

Бот використовується тільки для доставки alert. Джерела й надалі читаються user-session,
тому бот не потрібно додавати до каналів або discussion-груп.

## Запуск

Локально:

```bash
telegram-monitor run
```

## Запуск в одну команду:

```bash
.venv/bin/telegram-monitor run
```

Або як довгоживучий Docker-сервіс:

```bash
docker compose up --build -d
docker compose logs -f telegram-monitor
```

Compose монтує локальний `config.toml` у `/app/config.toml` лише для читання та named volume
у `/app/.state`, тому підписники й Bot API offset не зникають після recreate контейнера.
Локальний обов'язковий `policy-prompt.txt` монтується read-only у
`/app/policy-prompt.txt`. Опціональний extension-файл не монтується стандартним
`compose.yaml`; для Compose передайте `AI_POLICY_PROMPT_EXTENDED_EXAMPLES` через локальне
environment-джерело або запустіть застосунок без контейнера, щоб він прочитав configured
`policy_prompt_extended_examples_path`. Поточний tracked bundle `prompts` входить до Docker
image, але обидва private policy-файли навмисно виключені через `.dockerignore`. Для
локального запуску SQLite зберігається в `.state/` проєкту.

Не публікуйте й не вставляйте в issue або chat output команди `docker compose config`: при
використанні `env_file` вона може відобразити розгорнуті secrets, а якщо policy задана через
`.env` — також повні `AI_POLICY_PROMPT` та `AI_POLICY_PROMPT_EXTENDED_EXAMPLES`.

Зупинка через `Ctrl+C` або `docker compose stop` коректно від'єднує Telegram-клієнт і
намагається доставити вже поставлені в чергу alerts.

## Логи подій

За стандартного `LOG_LEVEL=INFO` події одразу виводяться в термінал, без окремого log-файлу:

Рівні `DEBUG` та `INFO` виводяться у `stdout` як звичайний текст. Кожен `WARNING`
виводиться у `stdout` як однорядковий JSON із `level = warn` і `message`, щоб Railway
зберігав його саме як warning. `ERROR` і `CRITICAL` залишаються у `stderr` та мають
severity `error`. One-shot команда `ai-check` є винятком: її stdout зарезервований для
result JSON, тому всі diagnostics цієї команди залишаються у `stderr`.
Логери OpenAI SDK, `httpx` і `httpcore` примусово обмежені рівнем `WARNING`, навіть якщо
application `LOG_LEVEL=DEBUG`, щоб request options, private policy та message text не
потрапили в debug output.

Точково приховано внутрішні Telethon `INFO`-логи `Got difference for channel <id> updates`
та `Got difference for account updates`. Решта `INFO`, `WARNING` та `ERROR` Telethon
залишається без змін.

```text
New user (chat_id=123, user_id=123, username=@example, first_name=Name)
Skip new message - 2026-08-06T12:29:00+03:00
Match new message - 2026-08-06T12:30:00+03:00: message text
Removed user (chat_id=123, user_id=123, username=@example, first_name=Name, reason=/stop)
Bot alert broadcast started (total=3)
Bot alert delivered to 3/3 subscriber(s)
Bot alert delivery incomplete (status=partial, delivered=2, failed=1, total=3, failed_chat_ids=456)
AI observation completed (decision=accept, elapsed_seconds=0.842, ...)
AI observation completed (decision=reject, reason_code=spam_or_scam, ...)
AI rejected notification; skipped Telegram delivery (message=chat/message, alert=...)
AI observation failed (status=timeout, model=gpt-5.4-nano-2026-03-17, elapsed_seconds=30.000, ...)
AI observation failed (status=invalid_response, ..., ai_response=...)
```

Логування відбувається після всіх перевірок. Якщо повідомлення закінчується на `?`,
позитивних збігів немає або спрацював `keywords_to_skip`, записується лише
`Skip new message - datetime` без тексту повідомлення. Лише після позитивного
match/`notify_all=True` і відсутності негативного match
записується `Match new message - datetime: preview`; у `bot`-режимі це також власні
outgoing-повідомлення. Повторний Telegram update з тим самим `(chat_id, message_id)` вдруге
не логується. Переноси рядків і керувальні символи matched-тексту прибираються, а довгий текст
скорочується. Автоматичне видалення користувача, який заблокував бота, має
`reason=unreachable`. Для `notify_all` або `skip_ai = true` після `Match` ідуть звичайні
delivery-логи, але не `AI observation completed/failed`, бо observer не викликається.

Тимчасова Bot API помилка має рівень `WARNING` і містить `chat_id`, `error_code`,
`retry_after` та номер спроби. Після вичерпання retries записуються два `ERROR`: детальна
помилка конкретного одержувача і підсумок broadcast. Підсумок прямо вказує, що подальший
автоматичний retry не запланований. Текст alert, Bot API payload і token у delivery-логи не
записуються.

Семантичний AI log містить `decision`, optional reject-only `reason_code`, timing у секундах
та безпечні metadata. Після `accept` Telegram delivery продовжується. Після `reject` delivery
не викликається, а весь сформований alert — message text, source, time, matches, AI reason і
посилання — записується одним санітизованим structured `WARNING`-рядком у `stdout` для Railway.
Технічний результат записується на рівні `ERROR`, але саме повідомлення проходить далі за
fail-open правилом.
Для `invalid_response` запис також містить однорядковий, обмежений за довжиною
`ai_response`; для інших результатів raw
response не додається. Message text, location, event, reason, exception, prompt і API key у
ці записи не додаються.

## Перевірка коду

```bash
.venv/bin/python -m pytest
.venv/bin/python -m pytest --cov --cov-report=term-missing
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Тести не підключаються до Telegram чи OpenAI, не потребують реальних credentials і
перевіряють nested TOML, межі значень, bundle validation, optional extension і стабільність
`prompt_hash`, бінарний parsing, nullable wire-поля, формування Responses API request,
retries, єдиний timeout budget і нормалізацію помилок, pipeline deduplication,
`notify_all`/`skip_ai` AI bypass, formatter та lifecycle повністю офлайн через fake
SDK/observer clients. `ai-check` тестується через injected fake client factory; pytest і CI
ніколи не запускають `ai-check --live` проти реального API.

## Межі цього MVP

- Обробляються нові повідомлення, але не подальші редагування.
- У `bot`-режимі і вхідні, і власні outgoing-повідомлення акаунта з monitored sources
  проходять детерміновані фільтри. Keyword-matched повідомлення за ввімкненого observation
  проходять бінарну класифікацію, якщо source не має `skip_ai = true`. `notify_all` і
  `skip_ai` повідомлення обходять AI та одразу доставляються; `skip_ai` на відміну від
  `notify_all` не скасовує keyword-вимогу. Для AI-eligible keyword path `accept` або technical
  failure доставляє alert, а `reject` лише записує його як `WARNING`. У `saved_messages`
  режимі outgoing і далі
  пропускаються, щоб повідомлення notifier-а не створили цикл самосповіщень.
- Сервіс має працювати постійно. Він не робить history backfill і тому не гарантує доставку
  повідомлень, які з'явилися, поки процес був вимкнений.
- Дедуплікація зберігається в RAM і скидається після рестарту.
- Черги навмисно обмежені. Якщо накопичиться понад 1000 недоставлених цікавих повідомлень
  або понад 5000 updates під час старту, overflow буде явно записано в error log, але
  частина подій не буде доставлена. Розміри можна змінити в `MonitorConfig`.
- Детерміновані фільтри визначають кандидатів для черги. Для keyword path із
  `skip_ai = false` рішення `accept` доставляється, `reject` іде лише у warning log, а
  technical statuses не блокують повідомлення за fail-open правилом. `notify_all` і
  `skip_ai = true` bypass не створюють
  AI request, рішення або AI-блок alert-а.
- Один послідовний notification worker зберігає порядок, але повільний 30-секундний AI
  timeout може затримати наступні alerts і збільшити ризик queue overflow.
- Для альбомів Telegram може надіслати кілька message updates; вони фільтруються та
  сповіщаються окремо.

Для AI-eligible keyword path гарантія одного AI observation діє в межах поточного in-memory
deduplication window. Після рестарту те саме Telegram-повідомлення потенційно може бути
класифіковане повторно; для абсолютної гарантії між рестартами потрібне окреме персистентне
сховище processed message IDs. `notify_all` і `skip_ai = true` повідомлення не
класифікуються.
