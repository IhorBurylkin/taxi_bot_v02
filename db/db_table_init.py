# db/db_table_init.py

import asyncpg
import asyncio
from log.log import log_info
from config.config import DB_DSN, TABLES_SCHEMAS

_pool = None

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
