import asyncpg
import asyncio
from log.log import log_info
from config.config import DB_DSN, TABLES_SCHEMAS
from typing import Optional
from dataclasses import dataclass
from datetime import datetime

_pool = None

@dataclass
class PoolStats:
    """Статистика connection pool"""
    timestamp: datetime
    size: int              # Текущий размер пула (созданные соединения)
    idle: int              # Простаивающих соединений
    busy: int              # Активных соединений (size - idle)
    min_size: int          # Минимум
    max_size: int          # Максимум
    available: int         # Резерв для роста (max_size - size)
    busy_pct: float        # % занятых от созданных (0-100)
    capacity_pct: float    # % использованной ёмкости от max_size (0-100)

async def create_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
        await log_info("Создан пул соединений PostgreSQL", type_msg="info")
    return _pool

async def get_connection():
    global _pool
    if _pool is None:
        await create_pool()
    return await _pool.acquire()

async def release_connection(connection):
    global _pool
    if _pool is not None:
        await _pool.release(connection)

async def close_pool():
    global _pool
    if _pool is None:
        return
    try:
        await asyncio.wait_for(_pool.close(), timeout=10)
        await log_info("Пул соединений PostgreSQL закрыт корректно", type_msg="info")
    except asyncio.TimeoutError:
        await log_info("Тайм-аут Pool.close(); принудительное завершение", type_msg="warning")
        try:
            _pool.terminate()
            await log_info("Пул PostgreSQL прекращен", type_msg="info")
        except Exception as e:
            await log_info(f"Ошибка во время pool.terminate(): {e}", type_msg="error")
    except Exception as e:
        await log_info(f"Неожиданная ошибка в pool.close(): {e}", type_msg="error")
    finally:
        _pool = None


async def _ensure_indexes(conn):
    """Создаём полезные индексы (idempotent)."""
    try:
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_order_date ON orders(order_date);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_driver_id ON orders(driver_id);")
    except asyncpg.PostgresError as e:
        await log_info(f"Ошибка создания индексов: {e}", type_msg="error")


async def _reconcile_orders_trip_start_default(conn):
    """
    Убеждаемся, что у orders.trip_start НЕТ DEFAULT now().
    Если есть — снимаем DEFAULT (ALTER COLUMN DROP DEFAULT).
    """
    try:
        q = """
        SELECT column_default
          FROM information_schema.columns
         WHERE table_schema='public' AND table_name='orders' AND column_name='trip_start';
        """
        default_expr = await conn.fetchval(q)
        if default_expr and "now()" in default_expr.lower():
            await conn.execute('ALTER TABLE "orders" ALTER COLUMN "trip_start" DROP DEFAULT;')
            await log_info("Снят DEFAULT now() у orders.trip_start", type_msg="info")
    except asyncpg.PostgresError as e:
        await log_info(f"Ошибка проверки DEFAULT у orders.trip_start: {e}", type_msg="error")


async def init_db_tables():
    """
    Проверка и инициализация таблиц PostgreSQL.
    Создаёт недостающие таблицы/колонки, чинит DEFAULT и ставит индексы.
    """
    connection = None
    try:
        connection = await get_connection()

        for table_name, columns in TABLES_SCHEMAS.items():
            # Проверка существования таблицы
            table_exists_query = """
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_schema = 'public' AND table_name = $1
                );
            """
            exists = await connection.fetchval(table_exists_query, table_name)

            if not exists:
                # Создание новой таблицы
                columns_def = ", ".join([f'"{col}" {col_type}' for col, col_type in columns.items()])
                create_query = f'CREATE TABLE "{table_name}" ({columns_def});'
                await connection.execute(create_query)
                await log_info(f"Создана таблица {table_name} с колонками: {columns_def}", type_msg="info")
            else:
                # Список существующих колонок
                columns_query = """
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_schema = 'public' AND table_name = $1;
                """
                existing_columns_rows = await connection.fetch(columns_query, table_name)
                existing_columns = {r["column_name"] for r in existing_columns_rows}

                # Добавляем недостающие
                for col, col_type in columns.items():
                    if col not in existing_columns:
                        alter_query = f'ALTER TABLE "{table_name}" ADD COLUMN "{col}" {col_type};'
                        try:
                            await connection.execute(alter_query)
                            await log_info(f"В таблицу {table_name} добавлена колонка {col} ({col_type})", type_msg="info")
                        except asyncpg.PostgresError as e:
                            await log_info(f"Ошибка при добавлении {col} в {table_name}: {e}", type_msg="error")

        # Доп. согласования для orders:
        await _reconcile_orders_trip_start_default(connection)
        await _ensure_indexes(connection)

    except Exception as e:
        await log_info(f"Ошибка инициализации таблиц: {e}", type_msg="error")
        raise
    finally:
        if connection:
            await release_connection(connection)

async def get_pool_stats() -> Optional[PoolStats]:
    """
    Получить статистику пула для мониторинга.

    Возвращает:
        PoolStats: если пул инициализирован.
        None: если пул не инициализирован.
    """
    global _pool
    if not _pool:
        return None
    
    size = _pool.get_size()          # Созданные соединения
    idle = _pool.get_idle_size()     # Простаивающие
    min_size = _pool.get_min_size()
    max_size = _pool.get_max_size()
    
    busy = idle                           # Активные
    available = max_size - size                  # Резерв роста
    busy_pct = (busy / max_size * 100) if size > 0 else 0        # % занятых
    capacity_pct = (size / max_size * 100) if max_size > 0 else 0  # % ёмкости
    
    return PoolStats(
        timestamp=datetime.now(),
        size=size,
        idle=idle,
        busy=busy,
        min_size=min_size,
        max_size=max_size,
        available=available,
        busy_pct=round(busy_pct, 1),
        capacity_pct=round(capacity_pct, 1)
    )


async def monitor_pool_health():
    """
    Фоновая задача для мониторинга здоровья пула.
    Запускать в main.py как asyncio.create_task()
    """
    previous_stats = 0

    while True:
        try:
            await asyncio.sleep(60)
            
            stats = await get_pool_stats()
            if not stats:
                continue
            
            async def log_info_pool():  
                await log_info(
                    f"[pool] size={stats.size}/{stats.max_size} "
                    f"(idle={stats.idle}, available={stats.available}) | "
                    f"busy={stats.busy_pct}%",
                    type_msg="info"
                )  


            changed = stats.busy_pct != previous_stats

            if changed:
                if stats.busy_pct >= 10 or stats.busy_pct == 0:
                    await log_info_pool()

                if 80 <= stats.busy_pct < 90:
                    await log_info(
                        f"⚠️ [pool] ВЫСОКАЯ ЗАНЯТОСТЬ: {stats.busy_pct}% "
                        f"({stats.size}/{stats.max_size} активны). "
                        f"Пул может вырасти до {stats.max_size} (доступно +{stats.available}).",
                        type_msg="warning"
                    )

            # Желательно: не спамить error, а триггерить только при смене состояния
            if stats.busy_pct >= 90 and changed:
                await log_info(
                    f"🚨 [pool] БЛИЗОК К ЛИМИТУ: {stats.busy_pct}% ёмкости "
                    f"({stats.size}/{stats.max_size}). Увеличьте DB_POOL_MAX_SIZE!",
                    type_msg="error"
                )

            previous_stats = stats.busy_pct
               
        except asyncio.CancelledError:
            break
        except Exception as e:
            await log_info(f"[pool] Ошибка мониторинга: {e}", type_msg="error")