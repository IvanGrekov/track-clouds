# Telegram Keyword Monitor

Невеликий event-driven сервіс, який працює від імені вашого звичайного Telegram-акаунта:

1. слухає лише явно налаштовані канали та групи;
2. перевіряє текст нового повідомлення або caption медіа;
3. шукає ключові слова чи фрагменти слів;
4. надсилає знайдене у Saved Messages або через окремого Telegram-бота.

На відміну від періодичного опитування MCP, сервіс тримає одне підключення до Telegram і
отримує `NewMessage` одразу після появи повідомлення. Він читає тільки ті діалоги, до яких
використаний акаунт уже має доступ; права адміністратора не потрібні.

Детальний опис компонентів і потоків даних: [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Що вже реалізовано

- окремі правила для кожного каналу або чату;
- `notify_all = true` для каналів, де потрібен кожен новий пост;
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
  строгий parser семантичної відповіді та ізольований асинхронний OpenAI Responses client
  з bounded retries і нормалізованими технічними результатами;
- observation інтегрований після всіх детермінованих фільтрів: він не змінює доставку,
  додає результат до того самого alert і не повторюється через Telegram delivery retries;
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

Приватний предметний prompt зберігайте в кореневому `policy-prompt.txt`. Як і
`config.toml`, цей файл ігнорується Git. Він потрібен для ввімкненого AI observation
або ручного `ai-check --live` і має містити поточну предметну policy у UTF-8.

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

Перевірити конфіг без Telegram-запитів:

```bash
telegram-monitor check "Шукаємо Kubernetes-інженера"
```

Повертається exit code `0`, якщо спрацювало хоча б одне правило, і `1`, якщо збігів немає.

## Конфігурація AI observation

AI observation вимкнений за замовчуванням і не змінює чинну детерміновану фільтрацію. Для
його ввімкнення додайте окрему таблицю **після всіх top-level settings і до першої**
`[[sources]]` (це важливо через правила scoping у TOML):

```toml
[ai_observation]
enabled = false
model = "gpt-5.4-nano-2026-03-17"
prompt_bundle_path = "prompts"
policy_prompt_path = "policy-prompt.txt"
default_trusted_area_context = "Львів"
operation_timeout_seconds = 30
request_attempts = 2
retry_base_seconds = 0.5
retry_max_seconds = 2.0
reasoning_effort = "none"
max_output_tokens = 800
store_responses = false

[[sources]]
peer = "@example_discussion"
keywords = ["хмар", "зелен"]
trusted_area_context = "Львів та околиці"
```

Основні правила конфігурації:

- `enabled = false` зберігає поточну поведінку й не вимагає API key або наявності bundle;
- `operation_timeout_seconds` обмежений 30 секундами й задає верхню межу. Клієнт отримує від
  caller-а залишок спільного timeout budget, обмежує його цим значенням і ділить між усіма
  HTTP attempts, retry-паузами, розбором та перевіркою відповіді; новий повний timeout для
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
- відносний `prompt_bundle_path` обчислюється від директорії самого `config.toml`, а не від
  поточної робочої директорії. Для `MONITOR_CONFIG_FILE` в іншому каталозі скопіюйте туди
  `prompts` або вкажіть абсолютний шлях;
- відносний `policy_prompt_path` так само обчислюється від директорії `config.toml`. За
  замовчуванням це приватний кореневий `policy-prompt.txt`, який є в `.gitignore`;
- `prompts` містить поточні `system-prompt.txt` і `response-format.json`; private
  policy завантажується окремо з файла або `AI_POLICY_PROMPT`;
- `response-format.json` містить поточний strict JSON Schema wrapper з іменем
  `telegram_mobility_observation`, придатний для передачі в
  Responses API як `text.format`. Локальний loader і parser перевіряють структуру та
  узгодженість результату відповідно до
  [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
  а ізольований client використовує цей формат у фактичному request;
- `prompt_hash` — повний SHA-256 від system prompt, фактично завантаженої приватної policy
  та канонічного JSON усього response format. Модель, API key, джерело policy, шлях, timeout
  і retries до hash не входять. Hash є fingerprint-ом фактично завантаженого вмісту,
  а не номером його версії;
- `OPENAI_API_KEY` створюється в OpenAI Platform і зберігається лише в `.env`, як описано в
  [офіційному quickstart](https://developers.openai.com/api/docs/quickstart#create-and-export-an-api-key).
  Валідатор перевіряє його наявність без повернення, показу чи логування значення.

Ізольований `AsyncOpenAI` Responses client формує request з поточних prompt-ів і вхідних
даних, виконує контрольовані retries в одному timeout budget, перевіряє Structured Output та
повертає типізоване рішення або нормалізований технічний статус. `timeout`, `rate_limited`,
`refusal`, `api_error` та `invalid_response` не перетворюються на штучний `review`.

Важливі semantic mappings:

- коли `prefilter.notify_all = true`, результат завжди має `decision = "accept"` і
  `reason_code = "notify_all_source"`; `location` заповнюється лише з тексту або
  `trusted_area_context`, `event` — лише з тексту, обидва поля можуть бути `null`,
  а temporal relevance залишається
  фактичним: `current`, `historical` або `unclear`;
- `notify_all_source` недопустимий, коли `prefilter.notify_all = false`; у звичайному
  keyword path `accept` вимагає `meets_all_criteria`, `current` і непорожні
  `location` та `event`;
- `unrelated_content` є reject-кодом;
- `no_location` і `no_event` можуть означати `review`, якщо текст потенційно корисний,
  але йому бракує контексту, або `reject`, якщо відсутній компонент однозначно
  робить повідомлення некорисним;
- `historical_context` завжди поєднується з `decision = "review"` і
  `temporal_relevance = "historical"`.

Коли observation увімкнений, notification worker викликає його лише після source/filter,
deduplication та automatic-forward перевірок. Уся AI-операція має один end-to-end budget
до 30 секунд. `accept`, `reject`, `review` і технічна помилка однаково
завершуються доставкою одного alert; Telegram retries повторюють уже сформований рядок і не
створюють нового OpenAI request. Створення client-а не виконує live API probe: credentials,
доступ до моделі та мережі фактично перевіряються під час першого request. Setup failure
нормалізується як fail-open `api_error`, щоб основна Telegram-доставка продовжилася.

Успішний alert містить блок `AI review:` із `Decision`, `Location`, `Event`,
`Relevance`, `Code reason`, `Reason` і загальним `Delay` у секундах з трьома знаками
після крапки, наприклад `Delay: 0.842 s`. `Delay` — це повна тривалість
observation поточного повідомлення від її початку до готового результату.
Model і token usage у Telegram не показуються, але залишаються доступними як безпечні
runtime metadata для application logs. Технічний блок містить лише `Status` та
однореченнєвий `Description`.

Semantic output є категоріальним: strict schema повертає `decision`, виявлені
location/event, temporal relevance, `reason_code` і короткий `reason`. Цей самий набір
семантичних полів використовують Telegram alert і manual CLI; application log залишає лише
`decision`, `reason_code` та безпечні metadata.

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
- `--notify-all` — змоделювати prefilter джерела з `notify_all = true`; цей path
  повертає `accept` з `reason_code = "notify_all_source"`;
- `--message-age-seconds N` — задати невід’ємний вік повідомлення; за
  замовчуванням `0`.

Потрібно передати хоча б один `--matched-keyword` або `--notify-all`, щоб
вхід відповідав production prefilter contract.

Якщо `[ai_observation].enabled = false`, `--live` одноразово активує лише manual
request у поточному процесі. Файл `config.toml` не змінюється, production observation
не вмикається. Для live request все одно потрібні чинні `OPENAI_API_KEY`,
prompt bundle і private policy. Команда не створює Telethon client, notifier чи
subscriber store, не надсилає Telegram alert і не змінює persistent state.

Успіх — це будь-яке валідне семантичне рішення: `accept`, `reject` або `review`.
Воно повертає exit code `0` і один JSON object у stdout:

```json
{
  "kind": "semantic",
  "decision": "accept",
  "location": "Липники, ліс у напрямку Львова",
  "event": "хмарно; згадані зелені",
  "temporal_relevance": "current",
  "reason_code": "meets_all_criteria",
  "reason": "Повідомлення описує актуальний стан маршруту.",
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

Технічний стан не маскується під `review`; він повертає exit code `3`:

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
HTTP attempts, retries, backoff, parsing і semantic validation. Це число в секундах; воно
не включає підготовку observation report, яка додатково враховується в `Delay` production
alert-а.

Exit code `2` означає невірні arguments або configuration/resources error, `130` —
переривання користувачем. Команда не додає до output API key, raw prompts, raw API
response, raw exception або окрему копію input envelope. Водночас нормалізовані поля
`location`, `event` і `reason` є вмістом відповіді моделі та можуть містити фрагменти
вхідного повідомлення. Токени в metadata є лише кількісною usage-статистикою. Якщо OpenAI
не повернув token usage, поле `token_usage` буде `null`.

### Приватна policy на Railway

У Railway додайте до service окрему variable з назвою `AI_POLICY_PROMPT` і вставте в неї
повний вміст локального `policy-prompt.txt` як звичайний багаторядковий текст:

```text
МЕТА
...
```

Не обгортайте значення в JSON, не замінюйте переноси рядків на текст `\n` і не кодуйте його
в Base64. Railway підтримує multiline variables: новий рядок можна вставити через
`Ctrl+Enter` / `Cmd+Enter` або Raw Editor. За потреби variable можна позначити як sealed.
[Railway documentation: multiline variables](https://docs.railway.com/variables#multiline-variables),
[sealed variables](https://docs.railway.com/variables#sealed-variables).
Після додавання або зміни variable перегляньте staged changes і виконайте deploy, інакше
running service не отримає нове значення.

Правила вибору джерела:

1. Якщо `AI_POLICY_PROMPT` присутня, використовується тільки її значення.
2. Якщо variable відсутня, читається локальний `policy_prompt_path`.
3. Порожня `AI_POLICY_PROMPT` є помилкою і не маскується fallback-файлом.
4. Якщо немає ні variable, ні файла, loader повертає безпечну configuration error без
   виведення policy в лог.

Таким чином Railway deployment не потребує policy у Git або Docker image. Після вставлення
того самого тексту hash буде однаковим локально й на Railway: loader нормалізує типи
переносів рядків і завершальний newline перед обчисленням fingerprint.

У runtime використовується поточне значення `AI_POLICY_PROMPT`. Доступ до Railway variable надавайте лише тим
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
Локальний `policy-prompt.txt` монтується read-only у `/app/policy-prompt.txt`. Поточний
tracked bundle `prompts` входить до Docker image, але private policy навмисно
виключена через `.dockerignore`. Для локального запуску SQLite зберігається в `.state/`
проєкту.

Не публікуйте й не вставляйте в issue або chat output команди `docker compose config`: при
використанні `env_file` вона може відобразити розгорнуті secrets, а якщо policy задана через
`.env` — також повний `AI_POLICY_PROMPT`.

Зупинка через `Ctrl+C` або `docker compose stop` коректно від'єднує Telegram-клієнт і
намагається доставити вже поставлені в чергу alerts.

## Логи подій

За стандартного `LOG_LEVEL=INFO` події одразу виводяться в термінал, без окремого log-файлу:

Рівні `DEBUG` та `INFO` виводяться у `stdout`, а `WARNING`, `ERROR` і `CRITICAL` — у
`stderr`. Завдяки цьому Railway не позначає звичайні інформаційні повідомлення як помилки.
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
AI observation completed (decision=accept, reason_code=meets_all_criteria, elapsed_seconds=0.842, ...)
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
`reason=unreachable`.

Тимчасова Bot API помилка має рівень `WARNING` і містить `chat_id`, `error_code`,
`retry_after` та номер спроби. Після вичерпання retries записуються два `ERROR`: детальна
помилка конкретного одержувача і підсумок broadcast. Підсумок прямо вказує, що подальший
автоматичний retry не запланований. Текст alert, Bot API payload і token у delivery-логи не
записуються.

Успішний AI log містить лише decision/reason code, timing у секундах та безпечні metadata;
технічний результат записується на рівні `ERROR`. Для `invalid_response` запис також
містить однорядковий, обмежений за довжиною `ai_response`; для інших результатів raw
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
перевіряють nested TOML, межі значень, bundle validation, стабільність `prompt_hash`,
строгий parsing, формування Responses API request, retries, єдиний timeout budget і
нормалізацію помилок, pipeline deduplication, formatter та lifecycle повністю
офлайн через fake SDK/observer clients. `ai-check` тестується через injected fake
client factory; pytest і CI ніколи не запускають `ai-check --live` проти реального API.

## Межі цього MVP

- Обробляються нові повідомлення, але не подальші редагування.
- У `bot`-режимі і вхідні, і власні outgoing-повідомлення акаунта з monitored sources
  проходять keyword-фільтр та створюють alerts. У `saved_messages` режимі outgoing і далі
  пропускаються, щоб повідомлення notifier-а не створили цикл самосповіщень.
- Сервіс має працювати постійно. Він не робить history backfill і тому не гарантує доставку
  повідомлень, які з'явилися, поки процес був вимкнений.
- Дедуплікація зберігається в RAM і скидається після рестарту.
- Черги навмисно обмежені. Якщо накопичиться понад 1000 недоставлених цікавих повідомлень
  або понад 5000 updates під час старту, overflow буде явно записано в error log, але
  частина подій не буде доставлена. Розміри можна змінити в `MonitorConfig`.
- Keyword filter залишається єдиним runtime-рішенням про доставку. AI observation лише
  пояснює, як класифікатор оцінив повідомлення, і ніколи не блокує alert.
- Один послідовний notification worker зберігає порядок, але повільний 30-секундний AI
  timeout може затримати наступні alerts і збільшити ризик queue overflow.
- Для альбомів Telegram може надіслати кілька message updates; вони фільтруються та
  сповіщаються окремо.

Гарантія одного AI observation діє в межах поточного in-memory deduplication window. Після
рестарту те саме Telegram-повідомлення потенційно може бути класифіковане повторно; для
абсолютної гарантії між рестартами потрібне окреме персистентне сховище processed message IDs.
