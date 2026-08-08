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
- повідомлення коротші за 10 символів після обрізання пробілів автоматично пропускаються;
- повідомлення, текст яких після обрізання кінцевих пробілів закінчується на `?`, пропускаються;
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

Заповніть у `.env` тільки `TELEGRAM_API_ID` та `TELEGRAM_API_HASH`, після чого створіть
окрему user-session:

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
відкидаються. Незалежно від правила, повідомлення коротше 10 символів після `strip()`
пропускається; рівно 10 символів уже проходить цю перевірку. Повідомлення, яке після
`rstrip()` закінчується на `?`, також завжди пропускається — навіть при збігу keyword або
`notify_all = true`. Правило без позитивних keywords дозволене лише з `notify_all = true`.

Перевірити конфіг без Telegram-запитів:

```bash
telegram-monitor check "Шукаємо Kubernetes-інженера"
```

Повертається exit code `0`, якщо спрацювало хоча б одне правило, і `1`, якщо збігів немає.

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
.venv/bin/telegram-monitor
```

Або як довгоживучий Docker-сервіс:

```bash
docker compose up --build -d
docker compose logs -f telegram-monitor
```

Compose монтує локальний `config.toml` у `/app/config.toml` лише для читання та named volume
у `/app/.state`, тому підписники й Bot API offset не зникають після recreate контейнера.
Для локального запуску вони зберігаються в `.state/` проєкту.

Зупинка через `Ctrl+C` або `docker compose stop` коректно від'єднує Telegram-клієнт і
намагається доставити вже поставлені в чергу alerts.

## Логи подій

За стандартного `LOG_LEVEL=INFO` події одразу виводяться в термінал, без окремого log-файлу:

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

## Перевірка коду

```bash
python -m pytest
python -m pytest --cov --cov-report=term-missing
ruff check .
```

Тести не підключаються до Telegram і не потребують реальних credentials.

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
- Keyword filter є точним substring match, а не семантичним AI-класифікатором.
- Для альбомів Telegram може надіслати кілька message updates; вони фільтруються та
  сповіщаються окремо.

Наступний логічний крок для production — SQLite checkpoints із backfill після рестарту, а
потім optional semantic classifier поверх уже дешевого keyword-фільтра.
