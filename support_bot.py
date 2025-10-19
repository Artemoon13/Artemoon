"""Telegram bot that enforces mutual support rules for Twitter influencers."""
from __future__ import annotations

import logging
import os
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telegram import ChatMember, ChatPermissions, Message, Update
from telegram.constants import ChatType
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackContext,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import Database
from twitter_client import TwitterAPIError, TwitterClient


REQUIRED_SUPPORTS = 10
DAILY_POST_LIMIT = 2

TWITTER_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:twitter|x)\.com/[^/\s]+/status/\d+(?:\?[^\s]*)?"
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BotConfig:
    token: str
    database_path: Path
    chat_id: Optional[int]
    twitter_bearer_token: str


def load_config() -> BotConfig:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in the environment or .env file")
    db_path = Path(os.environ.get("SUPPORT_BOT_DB", "data/support-bot.db"))
    chat_id_raw = os.environ.get("SUPPORT_CHAT_ID")
    chat_id = int(chat_id_raw) if chat_id_raw else None
    twitter_token = os.environ.get("TWITTER_BEARER_TOKEN")
    if not twitter_token:
        raise RuntimeError("Set TWITTER_BEARER_TOKEN for Twitter verification")
    return BotConfig(
        token=token,
        database_path=db_path,
        chat_id=chat_id,
        twitter_bearer_token=twitter_token,
    )


def normalise_twitter_handle(handle: str) -> str:
    handle = handle.strip()
    if not handle:
        raise ValueError("Twitter handle must not be empty")
    if handle.startswith("https://"):
        handle = handle.rsplit("/", 1)[-1]
    if handle.startswith("@"):
        handle = handle[1:]
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        raise ValueError("Twitter handle должен содержать только латинские буквы, цифры и подчёркивание")
    return "@" + handle


def normalise_tweet_url(url: str) -> str:
    match = TWITTER_URL_RE.search(url)
    if not match:
        raise ValueError("Не удалось распознать ссылку на твиттер пост")
    return match.group(0)


def extract_tweet_id(tweet_url: str) -> str:
    match = TWITTER_URL_RE.search(tweet_url)
    if not match:
        raise ValueError("Не удалось распознать ссылку на твиттер пост")
    segment = match.group(0).rsplit("/", 1)[-1]
    tweet_id = segment.split("?", 1)[0]
    return tweet_id


def extract_single_tweet_url(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("Отправь только одну ссылку на твит без дополнительного текста.")
    return normalise_tweet_url(lines[0])


def current_datetime() -> datetime:
    return datetime.now(timezone.utc)


def current_date_str() -> str:
    return current_datetime().date().isoformat()


async def _post_init(application: Application) -> None:
    await application.bot.set_my_commands(COMMANDS)


async def _post_shutdown(application: Application) -> None:
    client: Optional[TwitterClient] = application.bot_data.get("twitter_client")
    if client:
        await client.aclose()


def build_application(config: BotConfig, database: Database) -> Application:
    application = (
        ApplicationBuilder()
        .token(config.token)
        .rate_limiter(AIORateLimiter())
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    application.bot_data["db"] = database
    application.bot_data["chat_id"] = config.chat_id
    application.bot_data["twitter_client"] = TwitterClient(config.twitter_bearer_token)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("register", register))
    application.add_handler(CommandHandler("twitter", set_twitter))
    application.add_handler(CommandHandler("support", support_post))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(
        MessageHandler(filters.ChatType.GROUPS, enforce_group_rules), group=1
    )
    application.add_handler(ChatMemberHandler(track_bot_admin, ChatMemberHandler.MY_CHAT_MEMBER))

    return application


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_private_command(update, context):
        return
    await ensure_user(update, context)
    await update.message.reply_text(
        "Привет! В групповом чате разрешены только ссылки на твиты.\n"
        "1. Укажи свой X (Twitter) ник: /twitter @username.\n"
        "2. Отмечай поддержку постов через /support <ссылка> — бот автоматически проверит лайк или ретвит и тоже отвечает только здесь, в личке.\n"
        "3. Когда поддержишь 10 свежих постов, опубликуй ссылку в группу без текста.\n"
        "Команда /status покажет прогресс и лимиты."
    )


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def set_twitter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await ensure_private_command(update, context):
        return
    await ensure_user(update, context)
    if not context.args:
        await update.message.reply_text("Укажи никнейм: /twitter @username")
        return
    try:
        handle = normalise_twitter_handle(context.args[0])
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    db = get_db(context)
    db.update_twitter_handle(update.effective_user.id, handle)
    await update.message.reply_text(f"Твиттер обновлён: {handle}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_private_command(update, context):
        return
    await ensure_user(update, context)
    db = get_db(context)
    user = db.get_user(update.effective_user.id)
    pending = db.supports_needed(update.effective_user.id, REQUIRED_SUPPORTS)
    await update.message.reply_text(
        "\n".join(
            [
                f"Твиттер: {user['twitter_handle'] or 'не указан'}",
                f"Постов сегодня: {db.posts_today(update.effective_user.id, current_date_str())}/{DAILY_POST_LIMIT}",
                (
                    "Нет долгов по поддержке."
                    if not pending
                    else "Нужно поддержать ещё {} постов:\n{}".format(
                        len(pending),
                        "\n".join(f"• {row['tweet_url']}" for row in pending),
                    )
                ),
            ]
        )
    )


async def support_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await ensure_private_command(update, context):
        return
    await ensure_user(update, context)
    if not context.args:
        await update.message.reply_text("Использование: /support <ссылка на твит> [доказательство]")
        return

    try:
        tweet_url = normalise_tweet_url(context.args[0])
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    proof = " ".join(context.args[1:]) or None

    db = get_db(context)
    post = db.get_post_by_url(tweet_url)
    if not post:
        await update.message.reply_text("Пост не найден в базе. Убедись, что ссылка из текущего чата.")
        return

    if post["telegram_id"] == update.effective_user.id:
        await update.message.reply_text("Нельзя отмечать поддержку собственного поста.")
        return

    user = db.get_user(update.effective_user.id)
    if not user or not user["twitter_handle"]:
        await update.message.reply_text("Сначала привяжи Twitter через /twitter.")
        return

    twitter_client = get_twitter_client(context)
    try:
        verification = await twitter_client.verify_support(
            user["twitter_handle"], extract_tweet_id(tweet_url)
        )
    except TwitterAPIError as exc:
        await update.message.reply_text(f"Не удалось проверить поддержку: {exc}")
        return

    if not verification.supported:
        await update.message.reply_text(
            "Я не нашёл подтверждения лайка или ретвита. Проверь, что ты поддержал пост и попробуй снова."
        )
        return

    db.record_support(
        update.effective_user.id,
        post["id"],
        proof,
        current_datetime(),
        verified=True,
        verification_method=verification.method,
    )
    remaining = db.supports_needed(update.effective_user.id, REQUIRED_SUPPORTS)

    if not remaining:
        await unmute_user(update, context)

    await update.message.reply_text(
        (
            "Спасибо! Осталось поддержать {} постов.".format(len(remaining))
            if remaining
            else "Требования выполнены — можешь публиковать свой пост."
        )
        + (
            f"\nАвтопроверка: {('лайк' if verification.method == 'like' else 'ретвит')}"
            if verification.method
            else ""
        )
    )


async def ensure_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    db = get_db(context)
    db.upsert_user(
        telegram_id=update.effective_user.id,
        username=update.effective_user.username,
        twitter_handle=None,
    )


async def enforce_group_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not update.effective_chat:
        return
    if update.effective_chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    if not update.effective_user or update.effective_user.is_bot:
        return

    await ensure_user(update, context)

    text = (message.text or message.caption or "").strip()
    if not text:
        await delete_message(message)
        await notify_user(
            context,
            update.effective_user,
            "В чате можно публиковать только ссылку на твит.",
        )
        return

    if text.startswith("/"):
        # Команда была отправлена в чат — её обработают другие хендлеры.
        return

    try:
        tweet_url = extract_single_tweet_url(text)
    except ValueError as exc:
        await delete_message(message)
        await notify_user(context, update.effective_user, str(exc))
        return

    await process_group_post(update, context, tweet_url)


async def ensure_private_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if chat and chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        if update.message:
            await delete_message(update.message)
        await notify_user(context, update.effective_user, (
            "Команды работают только в личном чате с ботом. Напиши ему сначала /start."
        ))
        return False
    return True


async def process_group_post(
    update: Update, context: ContextTypes.DEFAULT_TYPE, tweet_url: str
) -> None:
    message = update.message
    if not message or not update.effective_user:
        return

    db = get_db(context)
    user_id = update.effective_user.id
    user = db.get_user(user_id)
    if not user or not user["twitter_handle"]:
        await delete_message(message)
        await notify_user(
            context,
            update.effective_user,
            "Сначала укажи твой аккаунт через /twitter в личке с ботом.",
        )
        return

    today = current_date_str()
    db.reset_daily_counter_if_needed(user_id, today)
    if db.posts_today(user_id, today) >= DAILY_POST_LIMIT:
        await delete_message(message)
        await notify_user(
            context,
            update.effective_user,
            "Лимит на публикации: максимум 2 ссылки в сутки.",
        )
        await mute_user(update, context)
        return

    pending = db.supports_needed(user_id, REQUIRED_SUPPORTS)
    if pending:
        await delete_message(message)
        await mute_user(update, context)
        await notify_user(
            context,
            update.effective_user,
            "Нужно поддержать ещё {} постов:\n{}".format(
                len(pending),
                "\n".join(f"• {row['tweet_url']}" for row in pending),
            ),
        )
        return

    post_id = db.record_post(
        telegram_id=user_id,
        tweet_url=tweet_url,
        message_id=message.message_id,
        created_at=current_datetime(),
    )
    db.increment_post_counter(user_id, today)
    await unmute_user(update, context)
    await notify_user(
        context,
        update.effective_user,
        "Пост сохранён (ID {}). Теперь поддержи свежие заявки, чтобы оставаться в очереди.".format(
            post_id
        ),
    )


async def delete_message(message: Message) -> None:
    with suppress(Exception):
        await message.delete()


async def notify_user(context: ContextTypes.DEFAULT_TYPE, user, text: str) -> None:
    if not text or not user:
        return
    user_id = getattr(user, "id", None)
    if user_id is None and isinstance(user, int):
        user_id = user
    if user_id is None:
        return
    with suppress(Exception):
        await context.bot.send_message(chat_id=user_id, text=text)


async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = resolve_chat_id(update, context)
    if chat_id is None:
        return
    permissions = ChatPermissions(can_send_messages=False)
    with suppress(Exception):
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=update.effective_user.id,
            permissions=permissions,
        )


async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = resolve_chat_id(update, context)
    if chat_id is None:
        return
    permissions = ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )
    with suppress(Exception):
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=update.effective_user.id,
            permissions=permissions,
        )


async def track_bot_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.my_chat_member:
        return
    status = update.my_chat_member.new_chat_member.status
    if status not in {ChatMember.ADMINISTRATOR, ChatMember.MEMBER}:
        logger.warning("Бот потерял доступ к чату %s", update.effective_chat.id)


def get_db(context: CallbackContext) -> Database:
    return context.application.bot_data["db"]


def get_twitter_client(context: CallbackContext) -> TwitterClient:
    return context.application.bot_data["twitter_client"]


def resolve_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    if update.effective_chat and update.effective_chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return update.effective_chat.id
    chat_id = context.application.bot_data.get("chat_id")
    return chat_id


COMMANDS = [
    ("start", "Информация и быстрая регистрация"),
    ("twitter", "Указать свой аккаунт X/Twitter"),
    ("support", "Отметить, что поддержал пост"),
    ("status", "Показать статус поддержек и лимиты"),
]


async def set_commands(application: Application) -> None:
    await application.bot.set_my_commands(COMMANDS)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = load_config()
    database = Database(config.database_path)
    application = build_application(config, database)

    logger.info("Запускаю бота...")
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        database.close()


if __name__ == "__main__":
    main()
