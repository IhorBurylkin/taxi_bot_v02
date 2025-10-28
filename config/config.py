from functools import lru_cache
from copy import deepcopy
import json

def load_config(file_path: str):
    try:
        with open(file_path, mode="r", encoding="utf-8") as f:
            content = f.read()
        print(f"Файл {file_path} успешно загружен.")    
        return json.loads(content)
    except FileNotFoundError:
        print(f"Файл {file_path} не найден.")
        return
    except json.JSONDecodeError as e:
        print(f"Ошибка декодирования JSON в файле: {file_path} ошибка: {e}")
        return
    except Exception as e:
        print(f"Неожиданная ошибка при загрузке файла {file_path} ошибка: {e}")
        return
    
@lru_cache(maxsize=2)
def _load_config_cached(file_path: str):
    """Внутренняя функция для кеширования загрузки"""
    return load_config(file_path)

def get_settings(file_path: str):
    """Возвращает глубокую копию конфига для безопасной мутации"""
    cached = _load_config_cached(file_path)
    return deepcopy(cached) if cached else None

config = get_settings("config/config.json")
lang_dict = get_settings("config/lang_dict.json")

LOGGING_FILE_PATH = config.get("LOGGING_FILE_PATH")
LOGGING_FILE_PATH_ADMINS = config.get("LOGGING_FILE_PATH_ADMINS")
LOGGING_SETTINGS_TO_SEND = config.get("LOGGING_SETTINGS_TO_SEND")
LOGGING_SETTINGS_TO_SEND_NEW_USERS = config.get("LOGGING_SETTINGS_TO_SEND_NEW_USERS")
LOGGING_SETTINGS_TO_SEND_ERRORS = config.get("LOGGING_SETTINGS_TO_SEND_ERRORS")
LOGGING_SETTINGS_TO_SEND_ORDERS = config.get("LOGGING_SETTINGS_TO_SEND_ORDERS")
LOGGING_SETTINGS_TO_SEND_SUPPORT = config.get("LOGGING_SETTINGS_TO_SEND_SUPPORT")
LOGGING_SETTINGS_TO_SEND_ADMINS = config.get("LOGGING_SETTINGS_TO_SEND_ADMINS")
LOGGING_SETTINGS_TO_SEND_SERVER_LOGS = config.get("LOGGING_SETTINGS_TO_SEND_SERVER_LOGS")
USERS_TABLE = config.get("USERS_TABLE")
ORDERS_TABLE = config.get("ORDERS_TABLE")
CONFIG_TABLE = config.get("CONFIG_TABLE")
TELEGRAM_BOT_TOKEN = config.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_BOT_TOKEN_ALTERNATIVE = config.get("TELEGRAM_BOT_TOKEN_ALTERNATIVE")
TELEGRAM_INFO_BOT_TOKEN = config.get("TELEGRAM_INFO_BOT_TOKEN")
TELEGRAM_INFO_BOT_TOKEN_ALTERNATIVE = config.get("TELEGRAM_INFO_BOT_TOKEN_ALTERNATIVE")
SUPPORTED_LANGUAGES = config.get("SUPPORTED_LANGUAGES")
DEFAULT_LANGUAGES = config.get("DEFAULT_LANGUAGES")
CITIES = config.get("CITIES")
COUNTRY_CHOICES = config.get("COUNTRY_CHOICES")
DB_DSN = config.get("DB_DSN")

STARS_ACCEPT_PRICE = config.get("STARS_ACCEPT_PRICE", 0)
STARS_ITEM_LABEL = config.get("STARS_ITEM_LABEL", "Accepting an order")

_BALANCE_PRESETS = config.get("_BALANCE_PRESETS")
STARS_MULTIPLIER = config.get("STARS_MULTIPLIER") 

MESSAGES = lang_dict.get("MESSAGES")

TABLES_SCHEMAS = {
    "users": {
        "user_id": "BIGSERIAL PRIMARY KEY",
        "username": "VARCHAR(50)",
        "first_name": "VARCHAR(100)",
        "language": "VARCHAR(10) DEFAULT 'en'",
        "theme_mode": "TEXT",  # 'light' | 'dark'
        "message_id": "BIGINT",
        "role": "VARCHAR(20) NOT NULL",
        "black_list": "BOOLEAN DEFAULT FALSE",
        "balance": "NUMERIC(10,2) DEFAULT 0.0",
        "country": "VARCHAR(100)",
        "region": "VARCHAR(100)",
        "city": "VARCHAR(100)",
        "phone_passenger": "VARCHAR(20)",
        "addresses_passenger": "JSONB",
        "rating_passenger": "NUMERIC(3,2)",
        "trips_count_passenger": "JSONB",
        "phone_driver": "VARCHAR(20)",
        "car_brand": "VARCHAR(100)",
        "car_model": "VARCHAR(100)",
        "car_color": "VARCHAR(50)",
        "car_number": "VARCHAR(20)",
        "car_image": "TEXT",
        "techpass_image": "TEXT",
        "driver_license": "TEXT",
        "rating_driver": "NUMERIC(3,2)",
        "trips_count_driver": "JSONB",
        "is_working": "BOOLEAN DEFAULT TRUE",
        "verified_driver": "BOOLEAN DEFAULT FALSE",
    },

    # trip_start — БЕЗ DEFAULT now(); добавлены временные точки и id ключевых сообщений
    "orders": {
        "order_id": "BIGSERIAL PRIMARY KEY",
        "message_id": "BIGINT",
        "order_date": "TIMESTAMPTZ NOT NULL",
        "passenger_id": "BIGINT NOT NULL REFERENCES users(user_id)",
        "driver_id": "BIGINT REFERENCES users(user_id)",
        "country": "VARCHAR(100)",
        "region": "VARCHAR(100)",
        "city": "VARCHAR(100)",
        "address_from": "TEXT NOT NULL",
        "address_to": "TEXT NOT NULL",

        # временные точки (для сводки и восстановления)
        "in_place_at": "TIMESTAMPTZ",          # водитель нажал «Я на месте»
        "scheduled_at": "TIMESTAMPTZ",     # запланированное время (если есть)
        "come_out_at": "TIMESTAMPTZ",          # пассажир нажал «Я выхожу»
        "auto_start_hint_at": "TIMESTAMPTZ",   # отправлен auto_start_hint
        "trip_start": "TIMESTAMPTZ",           # фактический старт (start_btn)
        "trip_end": "TIMESTAMPTZ",             # фактический конец

        "distance_km": "NUMERIC(10,2)",
        "cost": "NUMERIC(10,2)",
        "commission": "NUMERIC(10,2)",

        "status": "VARCHAR(20) NOT NULL",
        "initiator_id": "BIGINT REFERENCES users(user_id)",
        "reason": "TEXT",

        # id ключевых сообщений (для редактирования/удаления и восстановления после рестартов)
        "driver_main_msg_id": "BIGINT",        # главное сообщение водителя
        "passenger_info_msg_id": "BIGINT",     # карточка пассажира
        "driver_comeout_msg_id": "BIGINT",     # «Пассажир выходит»
        "driver_auto_hint_msg_id": "BIGINT"    # auto_start_hint
    },

    # снапшоты рантайма между рестартами
    "bot_runtime": {
        "key": "TEXT PRIMARY KEY",             # имя снапшота (например, ARRIVED_AT)
        "payload": "JSONB NOT NULL",           # произвольный JSON-словарь
        "updated_at": "TIMESTAMPTZ NOT NULL DEFAULT now()"
    },

    "config": {
        "id": "SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1)",   # одна запись-конфиг (singleton)

        "cities": "JSONB NOT NULL DEFAULT '[]'::jsonb",           # список городов/настроек по городам
        "country_choices": "JSONB NOT NULL DEFAULT '[]'::jsonb",   # список стран/настроек по странам
        "stars_enabled": "BOOLEAN NOT NULL DEFAULT FALSE",        # включение оплаты через Telegram Stars

        "check_country": "BOOLEAN NOT NULL DEFAULT FALSE",
        "region_in_bot": "BOOLEAN NOT NULL DEFAULT TRUE",

        # имя оставлено как в вашем коде (опечатка намеренно сохранена)
        "recruitment_scan_intervel": "INTEGER NOT NULL DEFAULT 30 CHECK (recruitment_scan_intervel > 0)",  # интервал сканирования, сек
        "recruitment_max_minutes": "INTEGER NOT NULL DEFAULT 15 CHECK (recruitment_max_minutes > 0)",      # максимум подбора, минут
        "commission_percent": "NUMERIC(5,2) NOT NULL DEFAULT 0 CHECK (commission_percent >= 0)",

        "updated_at": "TIMESTAMPTZ NOT NULL DEFAULT now()"        # техническое поле обновления
    }

}
