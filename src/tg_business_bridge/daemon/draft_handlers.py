import asyncio
import json
import logging
import sqlite3
import time
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)

from tg_business_bridge import db
from tg_business_bridge.config import Settings
from tg_business_bridge.sender import send_business_reply

log = logging.getLogger(__name__)
router = Router()


def _card_markup(draft_id: int, settings: Settings) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text="✅ Отправить", callback_data=f"draft:{draft_id}:approve")]
    if settings.editor_url:
        buttons.append(
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"draft:{draft_id}:edit")
        )
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


_CARD_LIMIT = 3500
_TRUNCATE_MARKER = "… [обрезано, полный текст будет отправлен]"


def _card_text(draft: sqlite3.Row, contact: str) -> str:
    # имя контакта приходит из Telegram и не ограничено схемой БД —
    # без обрезки длинное имя съедает лимит и карточка может превысить 4096
    if len(contact) > 64:
        contact = contact[:63] + "…"
    header = f"Черновик ответа для {contact} (chat {draft['chat_id']}):\n\n"
    body = header + draft["text"]
    if len(body) <= _CARD_LIMIT:
        return body
    limit = max(0, _CARD_LIMIT - len(header) - len(_TRUNCATE_MARKER))
    return header + draft["text"][:limit] + _TRUNCATE_MARKER


# До этого момента (time.monotonic) карточки не отправляются: Telegram попросил
# подождать, преждевременные повторы только продлевают flood-лимит
_flood_wait_until = 0.0


def _contact_name(conn: sqlite3.Connection, chat_id: int) -> str:
    return db.last_incoming_sender_name(conn, chat_id) or str(chat_id)


async def _send_card(
    bot: Bot, conn: sqlite3.Connection, connection: sqlite3.Row, draft: sqlite3.Row,
    settings: Settings,
) -> None:
    contact = _contact_name(conn, draft["chat_id"])
    text = _card_text(draft, contact)
    sent = await bot.send_message(
        chat_id=connection["owner_id"], text=text, reply_markup=_card_markup(draft["id"], settings)
    )
    db.set_draft_card(conn, draft["id"], sent.message_id)
    db.set_draft_status(conn, draft["id"], "awaiting")

    for old in db.supersede_awaiting(conn, draft["chat_id"], draft["id"]):
        if old["card_message_id"] is None:
            continue
        old_text = _card_text(old, contact)
        try:
            await bot.edit_message_text(
                chat_id=connection["owner_id"],
                message_id=old["card_message_id"],
                text=old_text + "\n\n⏭ Заменён новым черновиком",
                reply_markup=None,
            )
        except Exception as exc:  # noqa: BLE001 - редактирование карточки не критично
            log.warning("не удалось отредактировать карточку черновика %s: %s", old["id"], exc)


async def process_new_drafts(bot: Bot, conn: sqlite3.Connection, settings: Settings) -> None:
    global _flood_wait_until
    connection = db.get_enabled_connection(conn)
    if connection is None:
        # Подключения (ещё) нет — черновики ждут, не помечаясь failed: bootstrap
        # в business_handlers подтянет connection при первом же входящем сообщении
        if db.get_drafts_by_status(conn, "pending") or db.get_drafts_by_status(conn, "approved"):
            log.warning("нет активного business connection — черновики ждут")
        return

    if time.monotonic() >= _flood_wait_until:
        for draft in db.get_drafts_by_status(conn, "pending"):
            try:
                await _send_card(bot, conn, connection, draft, settings)
            except TelegramRetryAfter as exc:
                _flood_wait_until = time.monotonic() + exc.retry_after
                log.warning(
                    "flood wait %s сек при отправке карточки черновика %s",
                    exc.retry_after, draft["id"],
                )
                break
            except Exception:  # noqa: BLE001 - сбойная карточка не должна блокировать остальные
                log.exception("не удалось отправить карточку черновика %s", draft["id"])

    for draft in db.get_drafts_by_status(conn, "approved"):
        if not db.claim_draft(conn, draft["id"]):
            continue  # уже забрано другим процессом/итерацией
        if len(draft["text"]) > 4096:
            res = {"ok": False, "error": "текст длиннее 4096 символов — Telegram не примет"}
        else:
            try:
                res = await send_business_reply(bot, conn, draft["chat_id"], draft["text"])
            except Exception as exc:  # noqa: BLE001 - черновик не должен зависнуть в 'sending'
                log.exception("непредвиденная ошибка отправки черновика %s", draft["id"])
                res = {"ok": False, "error": f"непредвиденная ошибка: {exc}"}
        if res["ok"]:
            db.set_draft_status(conn, draft["id"], "sent")
        else:
            db.set_draft_status(conn, draft["id"], "failed", res["error"])

        if draft["card_message_id"]:
            base_text = _card_text(draft, _contact_name(conn, draft["chat_id"]))
            suffix = "\n\n✅ Отправлено" if res["ok"] else f"\n\n⚠️ Не удалось отправить: {res['error']}"
            try:
                await bot.edit_message_text(
                    chat_id=connection["owner_id"],
                    message_id=draft["card_message_id"],
                    text=base_text + suffix,
                )
            except Exception as exc:  # noqa: BLE001 - редактирование карточки не критично
                log.warning("не удалось отредактировать карточку черновика %s: %s", draft["id"], exc)


async def watch_drafts(
    bot: Bot, conn: sqlite3.Connection, settings: Settings, interval: float = 3.0,
) -> None:
    while True:
        try:
            await process_new_drafts(bot, conn, settings)
        except Exception:  # noqa: BLE001 - цикл не должен умирать
            log.exception("draft watcher iteration failed")
        await asyncio.sleep(interval)


async def _drop_editor_keyboard(bot: Bot, chat_id: int) -> None:
    """Reply-клавиатуру снимает только сообщение с ReplyKeyboardRemove — отправляем
    минимальное и сразу удаляем, чтобы в диалоге не осталось служебной строки."""
    try:
        stub = await bot.send_message(chat_id=chat_id, text="✂️", reply_markup=ReplyKeyboardRemove())
        await bot.delete_message(chat_id=chat_id, message_id=stub.message_id)
    except Exception as exc:  # noqa: BLE001 - уборка чата не критична
        log.warning("не удалось убрать клавиатуру редактора: %s", exc)


async def _forget_edit_prompt(
    bot: Bot, conn: sqlite3.Connection, chat_id: int, draft: sqlite3.Row,
) -> None:
    """Удаляет приглашение открыть редактор и забывает его id."""
    if draft["edit_prompt_message_id"] is None:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=draft["edit_prompt_message_id"])
    except Exception as exc:  # noqa: BLE001 - уборка чата не критична
        log.warning("не удалось удалить приглашение черновика %s: %s", draft["id"], exc)
    db.set_edit_prompt(conn, draft["id"], None)


async def _cleanup_editor_ui(
    bot: Bot, conn: sqlite3.Connection, chat_id: int, draft: sqlite3.Row,
) -> None:
    """Убирает следы редактора, когда судьба черновика решена мимо него (отправка,
    отклонение). Без записанного приглашения убирать нечего: редактор не открывали."""
    if draft["edit_prompt_message_id"] is None:
        return
    await _forget_edit_prompt(bot, conn, chat_id, draft)
    await _drop_editor_keyboard(bot, chat_id)


@router.callback_query(F.data.startswith("draft:"))
async def on_draft_callback(
    cb: CallbackQuery, conn: sqlite3.Connection, bot: Bot, settings: Settings,
) -> None:
    # Validate callback data format before any DB operations
    parts = cb.data.split(":")
    if len(parts) != 3:
        await cb.answer()
        return

    _, draft_id_s, action = parts

    # Validate draft_id is all digits
    if not draft_id_s.isdigit():
        await cb.answer()
        return

    # Validate action is in allowed set ('reject' — с карточек, разосланных до появления
    # кнопки «Редактировать»: они должны продолжать работать)
    if action not in {"approve", "reject", "edit"}:
        await cb.answer()
        return

    draft_id = int(draft_id_s)
    draft = db.get_draft(conn, draft_id)
    if draft is not None and draft["status"] == "superseded":
        await cb.answer("Черновик уже неактуален")
        return
    if draft is None or draft["status"] != "awaiting":
        await cb.answer("Черновик уже обработан")
        return
    if action == "edit":
        if not settings.editor_url:
            # карточка могла быть разослана до того, как редактор выключили
            await cb.answer("Редактор недоступен")
            return
        # sendData умеет только web_app-кнопка reply-клавиатуры, поэтому редактор
        # открывается вторым шагом. Статус не трогаем: черновик остаётся 'awaiting',
        # владелец может передумать и отправить его с карточки как есть.
        url = f"{settings.editor_url}#id={draft_id}&text={quote(draft['text'])}"
        if draft["edit_prompt_message_id"] is not None:
            # повторные нажатия «Редактировать» не должны копить приглашения в диалоге
            try:
                await bot.delete_message(
                    chat_id=cb.message.chat.id, message_id=draft["edit_prompt_message_id"]
                )
            except Exception as exc:  # noqa: BLE001 - уборка чата не критична
                log.warning("не удалось удалить приглашение черновика %s: %s", draft_id, exc)
        try:
            sent = await bot.send_message(
                chat_id=cb.message.chat.id,
                text=f"✏️ Редактирование черновика №{draft_id} — открой редактор кнопкой ниже",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[
                        KeyboardButton(text="✏️ Открыть редактор", web_app=WebAppInfo(url=url))
                    ]],
                    resize_keyboard=True, one_time_keyboard=True,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - без ответа на callback юзер видит вечный спиннер
            # длинный черновик после urlencode раздувает URL кнопки — Telegram может его не принять
            log.warning("не удалось открыть редактор черновика %s: %s", draft_id, exc)
            await cb.answer(
                "Не удалось открыть редактор — черновик слишком длинный или произошла ошибка"
            )
            return
        db.set_edit_prompt(conn, draft_id, sent.message_id)
        await cb.answer()
    elif action == "approve":
        # Порядок обязателен: карточку правим в ⏳ ДО флипа в 'approved'. Как только статус
        # станет 'approved', вотчер может успеть отправить черновик и поставить финальное
        # «✅ Отправлено» — запоздавшая правка ⏳ перекрыла бы его. Гвардированный флип
        # (WHERE status='awaiting') закрывает TOCTOU с supersede_awaiting: если флип не
        # удался, черновик уже заменён/обработан — возвращаем карточке актуальное состояние.
        try:
            await cb.message.edit_text(text=cb.message.text + "\n\n⏳ Отправляю…", reply_markup=None)
        except Exception as exc:  # noqa: BLE001 - редактирование карточки не критично
            log.warning("не удалось отредактировать карточку черновика %s: %s", draft_id, exc)
        if not db.set_draft_status_if(conn, draft_id, "awaiting", "approved"):
            await cb.answer("Черновик уже неактуален")
            try:
                await cb.message.edit_text(
                    text=cb.message.text + "\n\n⏭ Черновик уже неактуален", reply_markup=None
                )
            except Exception as exc:  # noqa: BLE001 - редактирование карточки не критично
                log.warning("не удалось отредактировать карточку черновика %s: %s", draft_id, exc)
            return
        await cb.answer("Отправляю")
        await _cleanup_editor_ui(bot, conn, cb.message.chat.id, draft)
    else:
        # Reject: вотчер никогда не трогает 'rejected', гонки с ним нет — флип первым.
        if not db.set_draft_status_if(conn, draft_id, "awaiting", "rejected"):
            await cb.answer("Черновик уже неактуален")
            return
        await cb.answer("Отклонено")
        try:
            await cb.message.edit_text(text=cb.message.text + "\n\n❌ Отклонено", reply_markup=None)
        except Exception as exc:  # noqa: BLE001 - редактирование карточки не критично
            log.warning("не удалось отредактировать карточку черновика %s: %s", draft_id, exc)
        await _cleanup_editor_ui(bot, conn, cb.message.chat.id, draft)


def _parse_editor_payload(raw: str) -> tuple[int, str] | None:
    """(draft_id, text) из данных мини-приложения или None, если пришёл мусор.
    Сам текст никогда не попадает в логи — это личная переписка."""
    try:
        payload = json.loads(raw)
        draft_id_s = str(payload["id"])
        text = payload["text"]
    except (ValueError, TypeError, KeyError):
        return None
    if not draft_id_s.isdigit() or not isinstance(text, str) or not text.strip():
        return None
    if len(text) > 4096:  # Telegram не примет такой ответ при отправке
        return None
    return int(draft_id_s), text


async def _repost_card(
    bot: Bot, conn: sqlite3.Connection, owner_id: int, draft: sqlite3.Row, settings: Settings,
) -> None:
    """Пересоздаёт карточку внизу диалога — после правки она должна быть последней.
    Статус не трогает: _send_card здесь не подходит, он ставит 'awaiting' и делает
    supersede, а черновик к этому моменту мог быть уже одобрен.

    Сначала новая карточка, только потом уборка старой: если отправка сорвётся,
    у владельца останется рабочая старая карточка, а не черновик вообще без кнопок."""
    text = _card_text(draft, _contact_name(conn, draft["chat_id"]))
    old_card_id = draft["card_message_id"]
    sent = await bot.send_message(
        chat_id=owner_id, text=text, reply_markup=_card_markup(draft["id"], settings)
    )
    db.set_draft_card(conn, draft["id"], sent.message_id)
    if old_card_id is None:
        return
    try:
        await bot.delete_message(chat_id=owner_id, message_id=old_card_id)
    except Exception as exc:  # noqa: BLE001 - не удалилась, так хотя бы гасим кнопки
        log.warning("не удалось удалить карточку черновика %s: %s", draft["id"], exc)
        try:
            await bot.edit_message_text(
                chat_id=owner_id, message_id=old_card_id,
                text=text + "\n\n⏭ Заменён обновлённой карточкой", reply_markup=None,
            )
        except Exception as exc:  # noqa: BLE001 - редактирование карточки не критично
            log.warning("не удалось отредактировать карточку черновика %s: %s", draft["id"], exc)


@router.message(F.web_app_data)
async def on_editor_result(
    msg: Message, conn: sqlite3.Connection, bot: Bot, settings: Settings,
) -> None:
    connection = db.get_enabled_connection(conn)
    if connection is None or msg.from_user is None:
        return
    if msg.from_user.id != connection["owner_id"]:
        # правки чужих аккаунтов молча игнорируем: черновики видит только владелец
        log.warning("web_app_data от постороннего пользователя %s — пропущено", msg.from_user.id)
        return

    # плашка «вы передали данные боту» — служебный шум, убираем при любом исходе
    try:
        await msg.delete()
    except Exception as exc:  # noqa: BLE001 - уборка чата не критична
        log.warning("не удалось удалить сообщение редактора: %s", exc)

    parsed = _parse_editor_payload(msg.web_app_data.data)
    if parsed is None:
        await msg.answer("Не удалось обработать данные редактора", reply_markup=ReplyKeyboardRemove())
        return
    draft_id, text = parsed

    if not db.update_draft_text(conn, draft_id, text):
        await msg.answer("Черновик уже неактуален", reply_markup=ReplyKeyboardRemove())
        return

    owner_id = connection["owner_id"]
    draft = db.get_draft(conn, draft_id)
    await _forget_edit_prompt(bot, conn, owner_id, draft)
    # клавиатуру снимаем безусловно: раз пришли данные редактора, она точно на экране,
    # а id приглашения может быть пуст у черновиков, застрявших в правке при обновлении
    await _drop_editor_keyboard(bot, owner_id)
    # отдельного «черновик обновлён» нет: подтверждение — сама свежая карточка внизу
    await _repost_card(bot, conn, owner_id, draft, settings)
