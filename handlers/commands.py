import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from uuid import uuid4

from aiogram import types, Router, F, Bot
from aiogram.filters import Command
from aiogram.enums import ChatType
from aiogram.types import Message, ReplyKeyboardRemove, PreCheckoutQuery

from db.db_utils import user_exists, insert_into_table, get_user_data, update_table
from log.log import log_info, send_info_msg
from keyboards.inline_kb_commands import get_start_inline_kb
from config.config import SUPPORTED_LANGUAGES, DEFAULT_LANGUAGES, USERS_TABLE, MESSAGES
from config.config_utils import lang_dict
from web.web_notify import notify_user

router = Router()

router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

async def _is_user_blocked(user_id: int) -> bool:
    """
    Унифицированная проверка блокировки пользователя.
    Поддерживает разные схемы: is_blocked/blocked/black_list/status == 'blocked'.
    """
    try:
        row = await get_user_data(USERS_TABLE, user_id)
        if not row:
            return False
        v = (
            row.get("is_blocked")
            or row.get("blocked")
            or row.get("black_list")
            or (row.get("status") == "blocked")
        )
        return bool(v)
    except Exception as e:
        # если не смогли прочитать — не блокируем по ошибке, но логируем
        await log_info(
            f"_is_user_blocked failed for {user_id}: {e}",
            type_msg="warning",
            user_id=user_id,
        )
        return False


def _load_transactions(raw: object) -> list[dict[str, Any]]:
    """Преобразует произвольные данные транзакций к списку словарей."""
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, Mapping)]
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(data, list):
            return [dict(item) for item in data if isinstance(item, Mapping)]
    return []


def _extract_order_from_payload(payload: str | None) -> tuple[str, dict[str, Any]]:
    """Возвращает order_id и исходный JSON-пейлоад."""
    if not payload:
        return "", {}
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
            if isinstance(data, Mapping):
                order_id = str(data.get("order_id") or "")
                return order_id, dict(data)
        except json.JSONDecodeError:
            pass
    return str(payload), {}

@router.message(Command("start"))
async def send_welcome(message: types.Message):
    try:
        user_id: int | None = message.from_user.id
        await log_info(
            f"Получена команда /start от пользователя {user_id}",
            type_msg="info",
            user_id=user_id,
        )
        chat_id = message.chat.id if message.chat.type == ChatType.PRIVATE else message.from_user.id
        user_lang = message.from_user.language_code
        lang = user_lang if user_lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGES

        user_id_exists = await user_exists(user_id)
        await log_info(
            f"Проверка существования пользователя {user_id} в БД: {'найден' if user_id_exists else 'не найден'}",
            type_msg="info",
            user_id=user_id,
        )

        if user_id_exists == False:
            user_data = {
                "user_id": user_id,
                "username": message.from_user.username,
                "first_name": message.from_user.first_name,
                "language": lang,
                "role": "unknown"
            }
            await insert_into_table(USERS_TABLE, user_data)
            await send_info_msg(text=f'Тип сообщения: Инфо\nНовый пользователь!\nUsername: {user_data["username"]}\nFirst name: {user_data["first_name"]}\nUser ID: {user_data["user_id"]}', type_msg_tg="new_users")
            await log_info(
                f'Новый пользователь Username: {user_data["username"]} '
                f'First name: {user_data["first_name"]} '
                f'User ID: {user_data["user_id"]}',
                type_msg="info",
                user_id=user_id,
            )

        if await _is_user_blocked(user_id):
            # локализация, если есть ключ; иначе — дефолтный текст
            text = (
                (MESSAGES.get(lang, {}) or {}).get("blocked_user_info")
                or "Ваш доступ к сервису временно заблокирован.\n"
                   "Если Вы считаете это ошибкой — напишите в поддержку командой /support."
            )
            await log_info(
                f"/start: user {user_id} is blocked → show blocked notice",
                type_msg="info",
                user_id=user_id,
            )
            await message.answer(text, reply_markup=ReplyKeyboardRemove())
            return

        await message.answer(
            MESSAGES[lang]["start_greeting"],
            reply_markup=get_start_inline_kb(lang),
        )

    except Exception as e:
        await log_info(
            f"Ошибка в send_welcome: {e}",
            type_msg="error",
            user_id=user_id,
        )
        raise


@router.pre_checkout_query()
async def handle_pre_checkout(pre_checkout: PreCheckoutQuery, bot: Bot) -> None:
    try:
        await bot.answer_pre_checkout_query(pre_checkout.id, ok=True)
        await log_info(
            f"[payments] подтверждён pre_checkout для payload={pre_checkout.invoice_payload}",
            type_msg="info",
            user_id=pre_checkout.from_user.id,
        )
    except Exception as error:
        await log_info(
            f"[payments][pre_checkout][ОШИБКА] {error}",
            type_msg="error",
            user_id=pre_checkout.from_user.id,
        )


@router.message(F.successful_payment)
async def handle_successful_payment(message: Message) -> None:
    user_id = message.from_user.id
    payment = message.successful_payment
    order_id, payload_dict = _extract_order_from_payload(payment.invoice_payload)

    try:
        user_row = await get_user_data(USERS_TABLE, user_id)
    except Exception as db_error:
        await log_info(
            f"[payments][success] не удалось получить пользователя: {db_error}",
            type_msg="error",
            user_id=user_id,
        )
        return

    if not user_row:
        await log_info(
            "[payments][success] пользователь не найден в БД",
            type_msg="warning",
            user_id=user_id,
        )
        return

    user_lang = (user_row.get("language") or message.from_user.language_code or DEFAULT_LANGUAGES)
    user_lang = (user_lang or DEFAULT_LANGUAGES).lower()

    transactions = _load_transactions(user_row.get("transactions"))
    existing_tx: dict[str, Any] | None = None
    if order_id:
        for tx in transactions:
            if str(tx.get("order_id")) == order_id:
                existing_tx = tx
                break

    amount_stars = int(payment.total_amount)
    now_dt = datetime.now(timezone.utc)
    performed_ts = int(now_dt.timestamp())

    if existing_tx is None:
        existing_tx = {
            "id": f"stars_{uuid4().hex[:10]}",
            "tg_transaction_id": None,
            "order_id": order_id or f"TOPUP-{user_id}-{performed_ts}",
            "user_id": user_id,
            "direction": "inbound",
            "status": "pending",
            "amount_stars": amount_stars,
            "amount": amount_stars,
            "currency": payment.currency,
            "title": lang_dict("profile_balance_topup_invoice_title", user_lang),
            "description": lang_dict("profile_balance_topup_invoice_description", user_lang),
            "peer_type": None,
            "peer_id": None,
            "tg_payload": payment.invoice_payload,
            "created_at": performed_ts,
            "completed_at": None,
            "performed_at": None,
            "invoice_url": None,
            "invoice_slug": None,
            "invoice_message_id": None,
            "is_refund": False,
            "raw": {},
        }
        transactions.append(existing_tx)

    already_processed = bool(
        str(existing_tx.get("status")).lower() == "succeeded"
        and existing_tx.get("performed_at")
    )

    if already_processed:
        await log_info(
            f"[payments][success] платёж уже обработан ранее order_id={order_id}",
            type_msg="warning",
            user_id=user_id,
        )
        try:
            await message.answer(lang_dict("profile_balance_topup_payment_duplicate", user_lang))
        except Exception:
            pass
        return

    existing_tx["status"] = "succeeded"
    existing_tx["amount_stars"] = amount_stars
    existing_tx["amount"] = existing_tx.get("amount") or amount_stars
    existing_tx["currency"] = payment.currency
    existing_tx["performed_at"] = performed_ts
    existing_tx["completed_at"] = performed_ts
    existing_tx["tg_transaction_id"] = payment.telegram_payment_charge_id
    if payment.provider_payment_charge_id:
        existing_tx["provider_payment_charge_id"] = payment.provider_payment_charge_id
    existing_tx.setdefault("raw", {})["successful_payment"] = payment.model_dump()
    if payload_dict:
        existing_tx["raw"]["payload"] = payload_dict

    balance_raw = user_row.get("balance")
    try:
        balance_decimal = Decimal(str(balance_raw)) if balance_raw not in (None, "") else Decimal("0")
    except (InvalidOperation, TypeError):
        balance_decimal = Decimal("0")

    balance_decimal += Decimal(amount_stars)
    balance_decimal = balance_decimal.quantize(Decimal("0.01"))

    updates = {
        "transactions": json.dumps(transactions, ensure_ascii=False),
        "balance": str(balance_decimal),
    }

    try:
        updated = await update_table(USERS_TABLE, user_id, updates)
    except Exception as update_error:
        await log_info(
            f"[payments][success] ошибка обновления БД: {update_error}",
            type_msg="error",
            user_id=user_id,
        )
        return

    if not updated:
        await log_info(
            "[payments][success] обновление пользователей вернуло False",
            type_msg="error",
            user_id=user_id,
        )
        return

    await log_info(
        f"[payments][success] платёж подтверждён, order_id={order_id}, amount={amount_stars}",
        type_msg="info",
        user_id=user_id,
    )

    paid_at_str = datetime.fromtimestamp(performed_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    user_tg_name = (
        (message.from_user.full_name or "").strip()
        or (user_row.get("first_name") or "").strip()
        or ""
    )
    user_tg_username = f"@{message.from_user.username}" if message.from_user.username else ""
    user_lang_db = user_row.get("language") or ""
    user_phone = user_row.get("phone_passenger") or user_row.get("phone_driver") or ""
    user_balance_str = str(balance_decimal)

    # делаем payload «красивым» для админа
    payload_pretty = ""
    if payload_dict:
        try:
            payload_pretty = json.dumps(payload_dict, ensure_ascii=False, indent=2)
        except Exception:
            payload_pretty = str(payload_dict)

    info_text = (
        "🟢 Платёж по Telegram Stars подтверждён\n"
        f"Пользователь: {user_id} {user_tg_username} {user_tg_name}\n"
        f"Язык пользователя: {user_lang_db or user_lang}\n"
        f"Телефон: {user_phone or '—'}\n"
        f"order_id: {existing_tx.get('order_id')}\n"
        f"Внутренний tx_id: {existing_tx.get('id')}\n"
        f"Статус: {existing_tx.get('status')}\n"
        f"Направление: {existing_tx.get('direction')}\n"
        f"Сумма (stars): {amount_stars}\n"
        f"Валюта: {payment.currency}\n"
        f"Баланс после операции: {user_balance_str}\n"
        f"tg_transaction_id: {existing_tx.get('tg_transaction_id')}\n"
        f"provider_payment_charge_id: {existing_tx.get('provider_payment_charge_id', '—')}\n"
        f"Время проведения: {paid_at_str}\n"
        f"Исходный invoice_payload: {payment.invoice_payload}\n"
        f"Распарсенный payload:\n{payload_pretty or '  —'}\n"
        f"Заголовок: {existing_tx.get('title')}\n"
        f"Описание: {existing_tx.get('description')}\n"
        "Источник: handle_successful_payment"
    )

    try:
        await send_info_msg(info_text, type_msg_tg="payments")
    except Exception as send_error:
        await log_info(
            f"[payments][success] send_info_msg error: {send_error}",
            type_msg="warning",
            user_id=user_id,
        )

    confirmation_text = lang_dict("profile_balance_topup_payment_success", user_lang, amount=str(amount_stars))

    try:
        await message.answer(confirmation_text)
    except Exception:
        pass

    try:
        await notify_user(user_id, confirmation_text, level="positive", position="center")
    except Exception as notify_error:
        await log_info(
            f"[payments][success] notify_user error: {notify_error}",
            type_msg="warning",
            user_id=user_id,
        )