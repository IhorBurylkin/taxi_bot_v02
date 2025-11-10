from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import asyncpg

from config.config import (
    SERVICE_COMMISSION_PERCENT,
    STAR_RATE,
    STARS_MULTIPLIER,
)
from db.db_table_init import get_connection, release_connection
from log.log import log_info


class WalletError(RuntimeError):
    """Базовая ошибка кошелька Stars."""


class InsufficientBalanceError(WalletError):
    """Недостаточно Stars на внутреннем балансе."""


class LedgerIntegrityError(WalletError):
    """Проблема при записи транзакции в users_transactions."""


@dataclass(slots=True)
class BalanceChange:
    """Результат изменения баланса."""

    tx_id: int
    balance_after: int


def _safe_decimal(value: Any) -> Decimal:
    """Преобразует значение к Decimal, возвращает 0 при ошибке."""
    try:
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def calc_commission_stars(cost: Decimal, currency: str) -> int:
    """Рассчитывает комиссию в Stars с округлением вверх."""
    try:
        percent = Decimal(str(SERVICE_COMMISSION_PERCENT or 0))
        if percent <= 0:
            return 0
        base_cost = _safe_decimal(cost)
        currency_key = (currency or "").upper() or "EUR"
        rate = _safe_decimal(STAR_RATE.get(currency_key, 1))
        multiplier = Decimal(str(STARS_MULTIPLIER or 1))
        raw = (base_cost * percent / Decimal("100")) * rate * multiplier
        return int(raw.quantize(Decimal("1"), rounding="ROUND_UP"))
    except Exception as error:  # noqa: BLE001
        # Логируем и считаем, что комиссия отсутствует, чтобы не блокировать заказ
        asyncio.create_task(
            log_info(
                "[wallet] ошибка расчёта комиссии, используем 0",
                type_msg="warning",
                reason=str(error),
            )
        )
        return 0


async def _ensure_connection(provided) -> tuple[asyncpg.Connection, bool]:
    if provided is not None:
        return provided, False
    connection = await get_connection()
    return connection, True


async def debit_commission(
    user_id: int,
    order_id: int,
    amount_stars: int,
    *,
    connection: Optional[asyncpg.Connection] = None,
    meta: Optional[dict[str, Any]] = None,
) -> BalanceChange:
    """Списывает Stars за комиссию и пишет проводку."""
    if amount_stars <= 0:
        raise WalletError("Сумма комиссии должна быть положительной")

    conn, owned = await _ensure_connection(connection)
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE users
                   SET balance = balance - $1,
                       balance_updated_at = NOW()
                 WHERE user_id = $2
                   AND balance >= $1
                RETURNING balance
                """,
                amount_stars,
                user_id,
            )
            if not row:
                raise InsufficientBalanceError("Недостаточно Stars")

            tx_row = await conn.fetchrow(
                """
                INSERT INTO users_transactions
                    (user_id, direction, amount_stars, reason, order_id, meta)
                VALUES ($1, 'debit', $2, 'commission', $3, $4::jsonb)
                RETURNING tx_id
                """,
                user_id,
                amount_stars,
                order_id,
                meta or {},
            )
            if not tx_row:
                raise LedgerIntegrityError("Не удалось записать транзакцию комиссии")

            balance_after = int(row["balance"])
            tx_id = int(tx_row["tx_id"])

            await log_info(
                "[wallet] комиссия списана со счёта водителя",
                type_msg="info",
                user_id=user_id,
                order_id=order_id,
                amount=int(amount_stars),
                tx_id=tx_id,
                balance_after=balance_after,
            )

            return BalanceChange(tx_id=tx_id, balance_after=balance_after)
    except InsufficientBalanceError:
        await log_info(
            "[wallet] недостаточно Stars для списания комиссии",
            type_msg="warning",
            user_id=user_id,
            order_id=order_id,
            amount=int(amount_stars),
        )
        raise
    except Exception as error:  # noqa: BLE001
        await log_info(
            "[wallet] ошибка при списании комиссии",
            type_msg="error",
            user_id=user_id,
            order_id=order_id,
            reason=str(error),
        )
        raise
    finally:
        if owned:
            await release_connection(conn)


async def credit_refund(
    user_id: int,
    order_id: int,
    related_tx_id: int,
    amount_stars: int,
    *,
    connection: Optional[asyncpg.Connection] = None,
    meta: Optional[dict[str, Any]] = None,
) -> BalanceChange:
    """Возвращает удержанную комиссию водителю."""
    if amount_stars <= 0:
        raise WalletError("Сумма возврата должна быть положительной")

    conn, owned = await _ensure_connection(connection)
    try:
        async with conn.transaction():
            balance_row = await conn.fetchrow(
                """
                UPDATE users
                   SET balance = balance + $1,
                       balance_updated_at = NOW()
                 WHERE user_id = $2
                RETURNING balance
                """,
                amount_stars,
                user_id,
            )
            if not balance_row:
                raise LedgerIntegrityError("Не удалось обновить баланс при возврате")

            tx_row = await conn.fetchrow(
                """
                INSERT INTO users_transactions
                    (user_id, direction, amount_stars, reason, order_id, related_tx_id, meta)
                VALUES ($1, 'credit', $2, 'refund', $3, $4, $5::jsonb)
                RETURNING tx_id
                """,
                user_id,
                amount_stars,
                order_id,
                related_tx_id,
                meta or {},
            )
            if not tx_row:
                raise LedgerIntegrityError("Не удалось записать транзакцию возврата")

            balance_after = int(balance_row["balance"])
            tx_id = int(tx_row["tx_id"])

            await log_info(
                "[wallet] комиссия возвращена водителю",
                type_msg="info",
                user_id=user_id,
                order_id=order_id,
                related_tx_id=related_tx_id,
                amount=int(amount_stars),
                tx_id=tx_id,
                balance_after=balance_after,
            )

            return BalanceChange(tx_id=tx_id, balance_after=balance_after)
    except Exception as error:  # noqa: BLE001
        await log_info(
            "[wallet] ошибка при возврате комиссии",
            type_msg="error",
            user_id=user_id,
            order_id=order_id,
            related_tx_id=related_tx_id,
            reason=str(error),
        )
        raise
    finally:
        if owned:
            await release_connection(conn)


async def credit_topup(
    user_id: int,
    amount_stars: int,
    *,
    order_id: Optional[int] = None,
    payload: str | None = None,
    connection: Optional[asyncpg.Connection] = None,
    meta: Optional[dict[str, Any]] = None,
) -> BalanceChange:
    """Зачисляет Stars на баланс (пополнение)."""
    if amount_stars <= 0:
        raise WalletError("Сумма пополнения должна быть положительной")

    conn, owned = await _ensure_connection(connection)
    try:
        async with conn.transaction():
            balance_row = await conn.fetchrow(
                """
                UPDATE users
                   SET balance = balance + $1,
                       balance_updated_at = NOW()
                 WHERE user_id = $2
                RETURNING balance
                """,
                amount_stars,
                user_id,
            )
            if not balance_row:
                raise LedgerIntegrityError("Не удалось обновить баланс при пополнении")

            tx_row = await conn.fetchrow(
                """
                INSERT INTO users_transactions
                    (user_id, direction, amount_stars, reason, order_id, meta)
                VALUES ($1, 'credit', $2, 'topup', $3, $4::jsonb)
                RETURNING tx_id
                """,
                user_id,
                amount_stars,
                order_id,
                {
                    "payload": payload,
                    "extra": meta or {},
                },
            )
            if not tx_row:
                raise LedgerIntegrityError("Не удалось записать транзакцию пополнения")

            balance_after = int(balance_row["balance"])
            tx_id = int(tx_row["tx_id"])

            await log_info(
                "[wallet] баланс пополнен через Stars",
                type_msg="info",
                user_id=user_id,
                order_id=order_id,
                amount=int(amount_stars),
                tx_id=tx_id,
                balance_after=balance_after,
            )

            return BalanceChange(tx_id=tx_id, balance_after=balance_after)
    except Exception as error:  # noqa: BLE001
        await log_info(
            "[wallet] ошибка при пополнении баланса",
            type_msg="error",
            user_id=user_id,
            order_id=order_id,
            reason=str(error),
        )
        raise
    finally:
        if owned:
            await release_connection(conn)
