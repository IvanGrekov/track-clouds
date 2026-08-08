"""MVP configuration.

Edit ``CONFIG`` directly for now. Credentials do not belong here; keep them in
``.env`` as described in the README.
"""

from .models import MonitorConfig, SourceRule

__all__ = ["CONFIG", "SourceRule"]

# fmt: off
CONFIG = MonitorConfig(
    sources=(
        # Filter a public discussion group by keyword or word fragment:
        # SourceRule(
        #     peer="@example_discussion",
        #     keywords=("kubernetes", "terraform", "ваканс"),
        #     keywords_to_skip=("spam", "реклама"),
        #     label="Example discussion",
        # ),
        #
        # Notify about every post in a public channel:
        # SourceRule(peer="@example_channel", notify_all=True),
        #
        # Private groups/channels have no username. Use the ID from `list-chats`:
        # SourceRule(peer=-1001234567890, keywords=("реліз", "incident")),
        SourceRule(
            peer="@holovni_Lviv",
            keywords=("надіслати прогноз",),
            keywords_to_skip=("доброго ранку",),
            label="Головний канал",
        ),
        SourceRule(
            peer=-1001719510902,
            keywords=(
                # ruff: ignore[E501]
                "пасут", "намоту", "стоят", "їздят", "їздит", "паку", "зупин", "пиня", "перевір", "перекри", "поїх", "катают", "катає",
                "кружля", "круг", "кол", "сторон",
                "бус", "т5", "дасте", "берлі", "транзит", "джип", "чорн", "біл", "сір", "блях",
                "паркінг", "парков", "підзем",
                "патр", "поліц", "син", "мусор", "фару", "фара", "швидкі",
                "хмар", "дощ", "полив", "ллє", "злив",
                "блок", "пост", "облав",
                "чист", "ніког",
                "тцк", "військ", "підар", "зелен",
            ),
            keywords_to_skip=(
                "реквізит", "@", "http", "услуг",
                "зарплат", "оплат", "оплач", "ваканс", "робот", "работ", "офіс", "ищем", "шукаєм",
                "бронюван", "допоможемо", "зняття", "знімаємо", "знімем",
            ),
            label="Головний чат",
        ),
    ),
    # Easiest setup: matched messages are copied to Telegram Saved Messages.
    # notification_mode="saved_messages",
    # notify_to="me",
    # For real push notifications, set TELEGRAM_BOT_TOKEN in .env. Users subscribe
    # to this bot with /start and unsubscribe with /stop.
    notification_mode="bot",
    bot_subscriber_limit=10,
    timezone="Europe/Kyiv",
)
# fmt: on
