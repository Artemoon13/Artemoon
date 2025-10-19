#!/usr/bin/env python3
"""Support routing Telegram bot.

This module implements a Telegram bot that receives messages from end users and
sends them to a moderator chat with inline controls allowing the moderator to
approve, delete, or block the user. When a request is approved the bot creates a
fresh invite link for the configured support group and shares it with the user.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import uuid
from typing import Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

LOGGER = logging.getLogger(__name__)

PENDING_REQUESTS_KEY = "pending_requests"
BLOCKED_USERS_KEY = "blocked_users"
TERMS_ACCEPTED_KEY = "accepted_terms"


@dataclasses.dataclass
class PendingRequest:
    """Stores information about a message awaiting moderator action."""

    request_id: str
    user_id: int
    user_chat_id: int
    user_message_id: int
    admin_message_id: int
    username: Optional[str]
    full_name: str
    text: str

    def format_display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.full_name


def _get_env(name: str, *, required: bool = True) -> Optional[str]:
    value = os.getenv(name)
    if required and not value:
        raise RuntimeError(
            f"Environment variable {name!r} must be set for the bot to work."
        )
    return value


def _get_pending_requests(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, PendingRequest]:
    return context.application_data.setdefault(PENDING_REQUESTS_KEY, {})  # type: ignore[return-value]


def _get_blocked_users(context: ContextTypes.DEFAULT_TYPE) -> set[int]:
    return context.application_data.setdefault(BLOCKED_USERS_KEY, set())  # type: ignore[return-value]


def _build_moderator_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Удалить", callback_data=f"delete:{request_id}"),
                InlineKeyboardButton("Заблокировать", callback_data=f"block:{request_id}"),
            ],
            [
                InlineKeyboardButton("Подтвердить", callback_data=f"confirm:{request_id}"),
            ],
        ]
    )


def _build_terms_keyboard(include_decline: bool = True) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton("Yes, I agree", callback_data="terms:accept")]]
    if include_decline:
        buttons.append(
            [InlineKeyboardButton("No, I disagree", callback_data="terms:decline")]
        )
    return InlineKeyboardMarkup(buttons)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a short help message when the bot is started."""

    message = update.effective_message
    if not message:
        return

    if update.effective_chat and update.effective_chat.id == context.bot_data.get(
        "admin_chat_id"
    ):
        await message.reply_text(
            "Этот бот перенаправляет обращения пользователей в этот чат. "
            "Используйте кнопки под сообщениями для модерации."
        )
        return

    context.user_data.pop(TERMS_ACCEPTED_KEY, None)

    await message.reply_text(
        "Do you agree that we will pay you 20% of the profit from your idea?",
        reply_markup=_build_terms_keyboard(),
    )


def _format_message_for_admin(message: Message, display_name: str) -> str:
    text = message.text or message.caption or "(без текста)"
    escaped_text = escape_markdown(text, version=2)
    escaped_name = escape_markdown(display_name, version=2)
    return (
        f'" {escaped_text}"'
        "\n\n"
        f"от кого ({escaped_name})"
    )


async def handle_incoming_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    if not message or not update.effective_user:
        return

    admin_chat_id: int = context.bot_data["admin_chat_id"]

    if message.chat_id == admin_chat_id:
        # Ignore moderator messages.
        return

    blocked_users = _get_blocked_users(context)
    if update.effective_user.id in blocked_users:
        LOGGER.info("Ignoring message from blocked user %s", update.effective_user.id)
        return

    if not context.user_data.get(TERMS_ACCEPTED_KEY):
        await message.reply_text(
            "Please confirm the 20% profit sharing terms with /start before sending your idea."
        )
        return

    request_id = uuid.uuid4().hex
    if update.effective_user.username:
        display_name = f"@{update.effective_user.username}"
    else:
        display_name = update.effective_user.full_name
    admin_text = _format_message_for_admin(message, display_name)

    admin_message = await context.bot.send_message(
        chat_id=admin_chat_id,
        text=admin_text,
        reply_markup=_build_moderator_keyboard(request_id),
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    pending_request = PendingRequest(
        request_id=request_id,
        user_id=update.effective_user.id,
        user_chat_id=message.chat_id,
        user_message_id=message.id,
        admin_message_id=admin_message.message_id,
        username=update.effective_user.username,
        full_name=update.effective_user.full_name,
        text=message.text or message.caption or "",
    )
    _get_pending_requests(context)[request_id] = pending_request

    LOGGER.info(
        "Captured new request %s from user %s", request_id, update.effective_user.id
    )

    await message.reply_text("Спасибо! Мы передали вашу идею администраторам.")


async def _delete_request_message(
    request: PendingRequest, context: ContextTypes.DEFAULT_TYPE
) -> None:
    try:
        await context.bot.delete_message(
            chat_id=context.bot_data["admin_chat_id"],
            message_id=request.admin_message_id,
        )
    except TelegramError as exc:
        LOGGER.warning("Failed to delete moderator message: %s", exc)


async def _notify_user(
    context: ContextTypes.DEFAULT_TYPE,
    user_chat_id: int,
    text: str,
) -> None:
    try:
        await context.bot.send_message(chat_id=user_chat_id, text=text)
    except TelegramError as exc:
        LOGGER.warning("Failed to notify user %s: %s", user_chat_id, exc)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    if query.data.startswith("terms:"):
        await query.answer()
        _, decision = query.data.split(":", 1)
        if decision == "accept":
            context.user_data[TERMS_ACCEPTED_KEY] = True
            await query.edit_message_text(
                "Great! Please send your idea."
            )
        elif decision == "decline":
            context.user_data[TERMS_ACCEPTED_KEY] = False
            await query.edit_message_text(
                "You declined the profit sharing terms. Send /start if you change your mind."
            )
        else:
            await query.answer("Unknown option", show_alert=True)
        return

    action, _, request_id = query.data.partition(":")
    pending_requests = _get_pending_requests(context)
    request = pending_requests.get(request_id)

    if not request:
        await query.answer("Запрос уже обработан", show_alert=True)
        return

    await query.answer()

    if action == "delete":
        await _delete_request_message(request, context)
        pending_requests.pop(request_id, None)
        LOGGER.info("Moderator deleted request %s", request_id)
        await _notify_user(
            context,
            request.user_chat_id,
            "Идея была отклонена.",
        )
        return

    if action == "block":
        blocked_users = _get_blocked_users(context)
        blocked_users.add(request.user_id)
        pending_requests.pop(request_id, None)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"Пользователь {request.format_display_name()} заблокирован."
        )
        await _notify_user(
            context,
            request.user_chat_id,
            "Ваша идея отклонена, и вы больше не можете отправлять идеи в этого бота.",
        )
        LOGGER.info("Moderator blocked user %s", request.user_id)
        return

    if action == "confirm":
        invite_link = await _create_support_invite(request, context)
        pending_requests.pop(request_id, None)
        await query.edit_message_reply_markup(reply_markup=None)
        if invite_link:
            await query.message.reply_text(
                (
                    "Создан чат Dep x "
                    f"{request.format_display_name()}: {invite_link}"
                )
            )
            await _notify_user(
                context,
                request.user_chat_id,
                "Ваша идея одобрена! Присоединяйтесь к чату: " f"{invite_link}",
            )
        else:
            await query.message.reply_text(
                "Не удалось создать ссылку для чата. Проверьте конфигурацию бота."
            )
            await _notify_user(
                context,
                request.user_chat_id,
                "Оператор готов помочь, но мы не смогли создать ссылку на чат."
            )
        LOGGER.info("Moderator confirmed request %s", request_id)
        return

    await query.answer("Неизвестное действие", show_alert=True)


async def _create_support_invite(
    request: PendingRequest, context: ContextTypes.DEFAULT_TYPE
) -> Optional[str]:
    support_chat_id: Optional[int] = context.bot_data.get("support_chat_id")
    if not support_chat_id:
        LOGGER.warning("SUPPORT_CHAT_ID is not configured")
        return None

    topic_name = f"Dep x {request.format_display_name()}".strip()

    try:
        await context.bot.create_forum_topic(
            chat_id=support_chat_id, name=topic_name[:128]
        )
    except TelegramError as exc:
        LOGGER.info("Could not create forum topic: %s", exc)

    try:
        invite = await context.bot.create_chat_invite_link(
            chat_id=support_chat_id,
            name=topic_name[:32] or "Dep session",
        )
    except TelegramError as exc:
        LOGGER.error("Failed to create invite link: %s", exc)
        return None

    return invite.invite_link


async def unblock_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_chat.id != context.bot_data.get(
        "admin_chat_id"
    ):
        return

    if not context.args:
        await update.message.reply_text("Использование: /unblock <user_id>")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Некорректный идентификатор пользователя")
        return

    blocked_users = _get_blocked_users(context)
    if user_id in blocked_users:
        blocked_users.remove(user_id)
        await update.message.reply_text(
            f"Пользователь {user_id} успешно разблокирован."
        )
    else:
        await update.message.reply_text("Пользователь не найден в списке блокировок.")


def build_application() -> Application:
    token = _get_env("BOT_TOKEN")
    admin_chat_id = int(_get_env("ADMIN_CHAT_ID"))
    support_chat_id_value = _get_env("SUPPORT_CHAT_ID", required=False)
    support_chat_id = int(support_chat_id_value) if support_chat_id_value else None

    application = (
        Application.builder()
        .token(token)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    application.bot_data["admin_chat_id"] = admin_chat_id
    if support_chat_id:
        application.bot_data["support_chat_id"] = support_chat_id

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("unblock", unblock_user))
    non_command_filter = filters.ALL & ~filters.COMMAND
    application.add_handler(
        MessageHandler(non_command_filter, handle_incoming_message)
    )
    application.add_handler(CallbackQueryHandler(handle_callback))

    return application


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    application = build_application()

    LOGGER.info("Starting support bot")
    application.run_polling()


if __name__ == "__main__":
    main()
