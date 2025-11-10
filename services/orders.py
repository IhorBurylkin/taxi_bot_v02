from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

import asyncpg

from config.config import SERVICE_COMMISSION_PERCENT
from db.db_table_init import get_connection, release_connection
from log.log import log_info
from services.wallet import (
    BalanceChange,
    InsufficientBalanceError,
    WalletError,
    calc_commission_stars,
    debit_commission,
)


class OrderUnavailableError(RuntimeError):
    """Заказ недоступен для принятия."""


class DriverNotFoundError(RuntimeError):
    """Водитель не найден в базе."""


@dataclass(slots=True)
class ReservationResult:
    """Результат резервирования заказа."""

    order_id: int
    passenger_id: int
    commission_stars: int
    status: str
    commission_tx_id: Optional[int]
    driver_balance: Optional[int]
    needs_topup: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _fetch_driver_balance(conn: asyncpg.Connection, driver_id: int) -> int:
    """Читает баланс водителя с блокировкой строки."""
    row = await conn.fetchrow(
        "SELECT balance FROM users WHERE user_id = $1 FOR UPDATE",
        driver_id,
    )
    if not row:
        raise DriverNotFoundError("Водитель не найден")
    return int(row["balance"])


async def _insert_order_event(
    conn: asyncpg.Connection,
    order_id: int,
    user_id: Optional[int],
    role: Optional[str],
    event: str,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """Фиксирует событие заказа; ошибки не мешают основному потоку."""
    try:
        await conn.execute(
            """
            INSERT INTO order_events (order_id, user_id, role, event, payload)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            order_id,
            user_id,
            role,
            event,
            payload or {},
        )
    except Exception as error:  # noqa: BLE001
        await log_info(
            "[orders] не удалось записать событие заказа",
            type_msg="warning",
            order_id=order_id,
            reason=str(error),
        )


async def reserve_order_atomic(order_id: int, driver_id: int) -> ReservationResult:
    """Атомарно назначает водителя и удерживает комиссию, если хватает Stars."""
    conn = await get_connection()
    try:
        async with conn.transaction():
            updated = await conn.fetchrow(
                """
                UPDATE orders
                   SET driver_id = $1,
                       status = 'awaiting_fee',
                       commission_tx_id = NULL,
                       accepted_at = NULL
                 WHERE order_id = $2
                   AND status = 'pending'
                   AND driver_id IS NULL
                RETURNING order_id, passenger_id, cost, commission, commission_stars
                """,
                driver_id,
                order_id,
            )

            if updated is None:
                existing = await conn.fetchrow(
                    """
                    SELECT passenger_id, status, driver_id, commission_stars, commission_tx_id
                      FROM orders
                     WHERE order_id = $1
                    """,
                    order_id,
                )
                if existing is None:
                    raise OrderUnavailableError("Заказ не найден")
                if int(existing.get("driver_id") or 0) != driver_id:
                    raise OrderUnavailableError("Заказ уже принят другим водителем")

                await log_info(
                    "[orders] возвращаем существующее состояние заказа",
                    type_msg="info",
                    order_id=order_id,
                    user_id=driver_id,
                    status=str(existing.get("status")),
                )
                return ReservationResult(
                    order_id=order_id,
                    passenger_id=int(existing["passenger_id"]),
                    commission_stars=int(existing.get("commission_stars") or 0),
                    status=str(existing.get("status")),
                    commission_tx_id=(
                        int(existing.get("commission_tx_id"))
                        if existing.get("commission_tx_id")
                        else None
                    ),
                    driver_balance=None,
                    needs_topup=str(existing.get("status")) == "awaiting_fee",
                )

            cost_value = Decimal(str(updated.get("cost") or 0))
            commission_cash = Decimal(str(updated.get("commission") or 0))
            commission_percent = Decimal(str(SERVICE_COMMISSION_PERCENT or 0))

            commission_stars = calc_commission_stars(cost_value, "EUR")
            if commission_stars < 0:
                commission_stars = 0

            if commission_cash <= 0 and commission_percent > 0:
                commission_cash = (
                    (cost_value * commission_percent) / Decimal("100")
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            await conn.execute(
                """
                UPDATE orders
                   SET commission_stars = $1,
                       commission = COALESCE($2, commission)
                 WHERE order_id = $3
                """,
                commission_stars,
                commission_cash,
                order_id,
            )

            await _insert_order_event(
                conn,
                order_id,
                user_id=driver_id,
                role="driver",
                event="awaiting_fee",
                payload={"commission_stars": commission_stars},
            )

            driver_balance = await _fetch_driver_balance(conn, driver_id)

            if commission_stars == 0:
                await conn.execute(
                    """
                    UPDATE orders
                       SET status = 'accepted',
                           accepted_at = NOW()
                     WHERE order_id = $1
                    """,
                    order_id,
                )
                await _insert_order_event(
                    conn,
                    order_id,
                    user_id=driver_id,
                    role="driver",
                    event="accepted",
                    payload={"commission_stars": 0},
                )
                await log_info(
                    "[orders] заказ принят без комиссии",
                    type_msg="info",
                    order_id=order_id,
                    user_id=driver_id,
                )
                return ReservationResult(
                    order_id=order_id,
                    passenger_id=int(updated["passenger_id"]),
                    commission_stars=0,
                    status="accepted",
                    commission_tx_id=None,
                    driver_balance=driver_balance,
                    needs_topup=False,
                )

            if driver_balance < commission_stars:
                await log_info(
                    "[orders] баланс водителя недостаточен, требуется пополнение",
                    type_msg="info",
                    order_id=order_id,
                    user_id=driver_id,
                    commission_stars=int(commission_stars),
                    driver_balance=int(driver_balance),
                )
                return ReservationResult(
                    order_id=order_id,
                    passenger_id=int(updated["passenger_id"]),
                    commission_stars=int(commission_stars),
                    status="awaiting_fee",
                    commission_tx_id=None,
                    driver_balance=driver_balance,
                    needs_topup=True,
                )

            try:
                change: BalanceChange = await debit_commission(
                    user_id=driver_id,
                    order_id=order_id,
                    amount_stars=int(commission_stars),
                    connection=conn,
                    meta={"source": "order_accept"},
                )
            except InsufficientBalanceError:
                await log_info(
                    "[orders] баланс изменился, переводим заказ в ожидание оплаты",
                    type_msg="warning",
                    order_id=order_id,
                    user_id=driver_id,
                )
                return ReservationResult(
                    order_id=order_id,
                    passenger_id=int(updated["passenger_id"]),
                    commission_stars=int(commission_stars),
                    status="awaiting_fee",
                    commission_tx_id=None,
                    driver_balance=driver_balance,
                    needs_topup=True,
                )
            except WalletError as error:
                await log_info(
                    "[orders] ошибка кошелька при списании комиссии",
                    type_msg="error",
                    order_id=order_id,
                    user_id=driver_id,
                    reason=str(error),
                )
                raise

            await conn.execute(
                """
                UPDATE orders
                   SET status = 'accepted',
                       commission_tx_id = $1,
                       accepted_at = NOW()
                 WHERE order_id = $2
                """,
                change.tx_id,
                order_id,
            )

            await _insert_order_event(
                conn,
                order_id,
                user_id=driver_id,
                role="driver",
                event="accepted",
                payload={
                    "commission_stars": int(commission_stars),
                    "commission_tx_id": change.tx_id,
                },
            )

            await log_info(
                "[orders] заказ принят и комиссия списана",
                type_msg="info",
                order_id=order_id,
                user_id=driver_id,
                commission_tx_id=change.tx_id,
                commission_stars=int(commission_stars),
            )

            return ReservationResult(
                order_id=order_id,
                passenger_id=int(updated["passenger_id"]),
                commission_stars=int(commission_stars),
                status="accepted",
                commission_tx_id=change.tx_id,
                driver_balance=change.balance_after,
                needs_topup=False,
            )
    except (OrderUnavailableError, DriverNotFoundError):
        raise
    except Exception as error:  # noqa: BLE001
        await log_info(
            "[orders] ошибка при резервировании заказа",
            type_msg="error",
            order_id=order_id,
            user_id=driver_id,
            reason=str(error),
        )
        raise
    finally:
        await release_connection(conn)


async def capture_commission_after_topup(order_id: int, driver_id: int) -> ReservationResult:
    """Пытается списать комиссию после пополнения Stars и перевести заказ в accepted."""
    conn = await get_connection()
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT passenger_id, status, driver_id, commission_stars, commission_tx_id
                  FROM orders
                 WHERE order_id = $1
                 FOR UPDATE
                """,
                order_id,
            )
            if not row:
                raise OrderUnavailableError("Заказ не найден")
            if int(row.get("driver_id") or 0) != driver_id:
                raise OrderUnavailableError("Заказ назначен другому водителю")

            status = str(row.get("status"))
            commission_stars = int(row.get("commission_stars") or 0)

            if status == "accepted":
                await log_info(
                    "[orders] комиссия уже списана, возврат текущего состояния",
                    type_msg="info",
                    order_id=order_id,
                    user_id=driver_id,
                )
                return ReservationResult(
                    order_id=order_id,
                    passenger_id=int(row["passenger_id"]),
                    commission_stars=commission_stars,
                    status="accepted",
                    commission_tx_id=(
                        int(row.get("commission_tx_id"))
                        if row.get("commission_tx_id")
                        else None
                    ),
                    driver_balance=None,
                    needs_topup=False,
                )

            if status != "awaiting_fee":
                raise OrderUnavailableError("Неверный статус заказа для списания комиссии")

            if commission_stars <= 0:
                await conn.execute(
                    """
                    UPDATE orders
                       SET status = 'accepted',
                           accepted_at = NOW(),
                           commission_tx_id = NULL
                     WHERE order_id = $1
                    """,
                    order_id,
                )
                await _insert_order_event(
                    conn,
                    order_id,
                    user_id=driver_id,
                    role="driver",
                    event="accepted",
                    payload={"commission_stars": 0},
                )
                return ReservationResult(
                    order_id=order_id,
                    passenger_id=int(row["passenger_id"]),
                    commission_stars=0,
                    status="accepted",
                    commission_tx_id=None,
                    driver_balance=None,
                    needs_topup=False,
                )

            driver_balance = await _fetch_driver_balance(conn, driver_id)

            try:
                change = await debit_commission(
                    user_id=driver_id,
                    order_id=order_id,
                    amount_stars=commission_stars,
                    connection=conn,
                    meta={"source": "awaiting_fee_capture"},
                )
            except InsufficientBalanceError:
                await log_info(
                    "[orders] повторное списание комиссии отклонено — не хватает Stars",
                    type_msg="warning",
                    order_id=order_id,
                    user_id=driver_id,
                    driver_balance=int(driver_balance),
                )
                return ReservationResult(
                    order_id=order_id,
                    passenger_id=int(row["passenger_id"]),
                    commission_stars=commission_stars,
                    status="awaiting_fee",
                    commission_tx_id=None,
                    driver_balance=driver_balance,
                    needs_topup=True,
                )

            await conn.execute(
                """
                UPDATE orders
                   SET status = 'accepted',
                       commission_tx_id = $1,
                       accepted_at = NOW()
                 WHERE order_id = $2
                """,
                change.tx_id,
                order_id,
            )

            await _insert_order_event(
                conn,
                order_id,
                user_id=driver_id,
                role="driver",
                event="accepted",
                payload={
                    "commission_stars": commission_stars,
                    "commission_tx_id": change.tx_id,
                },
            )

            await log_info(
                "[orders] комиссия успешно списана после пополнения",
                type_msg="info",
                order_id=order_id,
                user_id=driver_id,
                commission_tx_id=change.tx_id,
            )

            return ReservationResult(
                order_id=order_id,
                passenger_id=int(row["passenger_id"]),
                commission_stars=commission_stars,
                status="accepted",
                commission_tx_id=change.tx_id,
                driver_balance=change.balance_after,
                needs_topup=False,
            )
    except (OrderUnavailableError, DriverNotFoundError):
        raise
    except Exception as error:  # noqa: BLE001
        await log_info(
            "[orders] ошибка при попытке списать комиссию после пополнения",
            type_msg="error",
            order_id=order_id,
            user_id=driver_id,
            reason=str(error),
        )
        raise
    finally:
        await release_connection(conn)
