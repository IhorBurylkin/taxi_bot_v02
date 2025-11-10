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
LOGGING_SETTINGS_TO_SEND_PAYMENTS = config.get("LOGGING_SETTINGS_TO_SEND_PAYMENTS")
GMAPS_API_KEY = config.get("GMAPS_API_KEY")
GMAPS_URL_SIGNING_SECRET = config.get("GMAPS_URL_SIGNING_SECRET")
GMAPS_CLIENT_ID = config.get("GMAPS_CLIENT_ID")
MAIN_DOMAIN = config.get("MAIN_DOMAIN")
USERS_TABLE = config.get("USERS_TABLE")
ORDERS_TABLE = config.get("ORDERS_TABLE")
CONFIG_TABLE = config.get("CONFIG_TABLE")
TELEGRAM_BOT_TOKEN = config.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_BOT_TOKEN_ALTERNATIVE = config.get("TELEGRAM_BOT_TOKEN_ALTERNATIVE")
TELEGRAM_INFO_BOT_TOKEN = config.get("TELEGRAM_INFO_BOT_TOKEN")
TELEGRAM_INFO_BOT_TOKEN_ALTERNATIVE = config.get("TELEGRAM_INFO_BOT_TOKEN_ALTERNATIVE")
SUPPORTED_LANGUAGES = config.get("SUPPORTED_LANGUAGES")
SUPPORTED_LANGUAGE_NAMES = config.get("SUPPORTED_LANGUAGE_NAMES")
DEFAULT_LANGUAGES = config.get("DEFAULT_LANGUAGES")
CITIES = config.get("CITIES")
COUNTRY_CHOICES = config.get("COUNTRY_CHOICES")
DB_DSN = config.get("DB_DSN")

STARS_ACCEPT_PRICE = config.get("STARS_ACCEPT_PRICE", 0)
STARS_ITEM_LABEL = config.get("STARS_ITEM_LABEL", "Accepting an order")
SERVICE_COMMISSION_PERCENT = config.get("SERVICE_COMMISSION_PERCENT", 0)
STAR_RATE = config.get("STAR_RATE", {})
ORDER_ACCEPT_TIMEOUT_SEC = config.get("ORDER_ACCEPT_TIMEOUT_SEC", 45)
AWAITING_FEE_TIMEOUT_SEC = config.get("AWAITING_FEE_TIMEOUT_SEC", 60)
WAIT_FREE_WINDOW_SEC = config.get("WAIT_FREE_WINDOW_SEC", 300)

_BALANCE_PRESETS = config.get("_BALANCE_PRESETS")
STARS_MULTIPLIER = config.get("STARS_MULTIPLIER")

TEST_TG_ACCOUNT_ID = config.get("TEST_TG_ACCOUNT_ID")

MESSAGES = lang_dict.get("MESSAGES")
TABLES_SCHEMAS = {
    "users": {
        "user_id": "BIGSERIAL PRIMARY KEY",
        "username": "VARCHAR(50)",
        "first_name": "VARCHAR(100)",
        "language": "VARCHAR(10) DEFAULT 'en'",
        "theme_mode": "TEXT",
        "message_id": "BIGINT",
        "role": "VARCHAR(20) NOT NULL",
        "black_list": "BOOLEAN DEFAULT FALSE",
        "balance": "BIGINT NOT NULL DEFAULT 0",
        "balance_updated_at": "TIMESTAMPTZ",
        "transactions": "JSONB",
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
        "verified_driver": "BOOLEAN DEFAULT FALSE"
    },
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
        "in_place_at": "TIMESTAMPTZ",
        "scheduled_at": "TIMESTAMPTZ",
        "come_out_at": "TIMESTAMPTZ",
        "auto_start_hint_at": "TIMESTAMPTZ",
        "trip_start": "TIMESTAMPTZ",
        "trip_end": "TIMESTAMPTZ",
        "distance_km": "NUMERIC(10,2)",
        "cost": "NUMERIC(10,2)",
        "commission": "NUMERIC(10,2)",
        "commission_stars": "BIGINT",
        "commission_tx_id": "BIGINT",
        "accepted_at": "TIMESTAMPTZ",
        "status": "VARCHAR(20) NOT NULL",
        "initiator_id": "BIGINT REFERENCES users(user_id)",
        "reason": "TEXT",
        "driver_main_msg_id": "BIGINT",
        "passenger_info_msg_id": "BIGINT",
        "driver_comeout_msg_id": "BIGINT",
        "driver_auto_hint_msg_id": "BIGINT"
    },
    "users_transactions": {
        "tx_id": "BIGSERIAL PRIMARY KEY",
        "user_id": "BIGINT NOT NULL REFERENCES users(user_id)",
        "created_at": "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        "direction": "VARCHAR(10) NOT NULL",
        "amount_stars": "BIGINT NOT NULL",
        "reason": "VARCHAR(30) NOT NULL",
        "order_id": "BIGINT REFERENCES orders(order_id)",
        "related_tx_id": "BIGINT",
        "meta": "JSONB"
    },
    "order_events": {
        "event_id": "BIGSERIAL PRIMARY KEY",
        "order_id": "BIGINT NOT NULL REFERENCES orders(order_id)",
        "user_id": "BIGINT REFERENCES users(user_id)",
        "role": "VARCHAR(20)",
        "event": "VARCHAR(50) NOT NULL",
        "payload": "JSONB",
        "created_at": "TIMESTAMPTZ NOT NULL DEFAULT NOW()"
    },
    "bot_runtime": {
        "key": "TEXT PRIMARY KEY",
        "payload": "JSONB NOT NULL",
        "updated_at": "TIMESTAMPTZ NOT NULL DEFAULT now()"
    },
    "config": {
        "id": "SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1)",
        "cities": "JSONB NOT NULL DEFAULT '[]'::jsonb",
        "country_choices": "JSONB NOT NULL DEFAULT '[]'::jsonb",
        "stars_enabled": "BOOLEAN NOT NULL DEFAULT FALSE",
        "check_country": "BOOLEAN NOT NULL DEFAULT FALSE",
        "region_in_bot": "BOOLEAN NOT NULL DEFAULT TRUE",
        "recruitment_scan_intervel": "INTEGER NOT NULL DEFAULT 30 CHECK (recruitment_scan_intervel > 0)",
        "recruitment_max_minutes": "INTEGER NOT NULL DEFAULT 15 CHECK (recruitment_max_minutes > 0)",
        "commission_percent": "NUMERIC(5,2) NOT NULL DEFAULT 0 CHECK (commission_percent >= 0)",
        "updated_at": "TIMESTAMPTZ NOT NULL DEFAULT now()"
    },
    "support_requests": {
        "user_id": "BIGSERIAL PRIMARY KEY",
        "messages": "JSONB"
    },
    "pending_notifications": {
        "user_id": "BIGINT PRIMARY KEY",
        "messages": "JSONB",
        "level": "VARCHAR(10)",
        "position": "VARCHAR(20)",
        "created_at": "TIMESTAMPTZ NOT NULL DEFAULT now()"
    }
}
