from __future__ import annotations

import json
import time

from typing import Any, Dict, List, Tuple, Optional
from db.db_table_init import get_connection, release_connection
from log.log import log_info
from config.config import get_settings, CITIES, COUNTRY_CHOICES

CONFIG_TABLE = "config"
CONFIG_ID = 1  # singleton-запись

DEFAULT_CONFIG = {
    "cities": CITIES,
    "country_choices": COUNTRY_CHOICES,
    "stars_enabled": False,
    "check_country": False,   # ← новое
    "region_in_bot": True,
    "recruitment_scan_intervel": 30,   # сек
    "recruitment_max_minutes": 3,      # мин
}

_config_cache: Optional[Dict[str, Any]] = None
_config_ts: float = 0.0
_CONFIG_TTL = 5.0

def _invalidate_cache():
    global _config_cache, _config_ts
    _config_cache, _config_ts = None, 0.0

async def _get_config_cached() -> Dict[str, Any]:
    """Лёгкий TTL-кеш одной строки config (id=1)."""
    global _config_cache, _config_ts
    now = time.monotonic()
    if _config_cache is not None and (now - _config_ts) < _CONFIG_TTL:
        return _config_cache

    # прежняя логика _fetch_config_row, но без ensure_config_exists() внутри
    conn = None
    try:
        conn = await get_connection()
        row = await conn.fetchrow(
            f"""
            SELECT id, cities, stars_enabled, recruitment_scan_intervel,
                   check_country, region_in_bot,
                   recruitment_max_minutes, updated_at,
                   country_choices
              FROM {CONFIG_TABLE}
             WHERE id = $1
             LIMIT 1
            """,
            CONFIG_ID,
        )
        _config_cache = dict(row) if row else {}
        _config_ts = now
        return _config_cache
    except Exception as e:
        await log_info(f"_get_config_cached failed: {e}", type_msg="error")
        return _config_cache or {}
    finally:
        if conn:
            await release_connection(conn)

def normalize_cities_tree(obj: Any) -> Dict[str, Dict[str, List[str]]]:
    """country -> region -> [cities] (всегда строки, без дублей, отсортировано)."""
    out: Dict[str, Dict[str, List[str]]] = {}
    if isinstance(obj, dict):
        for country, regions in obj.items():
            c = str(country).strip()
            if not c: 
                continue
            out.setdefault(c, {})
            if isinstance(regions, dict):
                for region, cities in regions.items():
                    r = str(region).strip()
                    if not r: 
                        continue
                    if isinstance(cities, list):
                        arr = sorted(set(_pick_name(it) for it in cities if _pick_name(it)), key=str.casefold)
                    elif isinstance(cities, str):
                        arr = [cities.strip()] if cities.strip() else []
                    else:
                        arr = []
                    out[c][r] = arr
            elif isinstance(regions, list):
                arr = sorted(set(_pick_name(it) for it in regions if _pick_name(it)), key=str.casefold)
                out[c][c] = arr
            elif isinstance(regions, str):
                r = regions.strip() or c
                out[c][r] = []
    elif isinstance(obj, list):
        # старый формат → одна виртуальная страна/регион
        arr = sorted(set(_pick_name(it) for it in obj if _pick_name(it)), key=str.casefold)
        out = {"Default": {"Default": arr}}
    return out


async def ensure_config_exists() -> Dict[str, Any]:
    """
    Гарантирует наличие строки config id=1 + приводит структуру.
    Синхронизация из config.json включается, если "SYNC_JSON_WITH_DB": true.
    """
    cfg = get_settings("config/config.json") or {}
    # defaults из файла (без ошибок, строго к ожидаемым типам)
    def _norm_cc(src):
        out = []
        if isinstance(src, (list, tuple)):
            for it in src:
                if not isinstance(it, (list, tuple)) or len(it) < 3:
                    continue
                code = str(it[0]).strip()
                flag = str(it[1]).strip()
                name = str(it[2]).strip()
                a = b = None
                if len(it) > 3:
                    try: a = int(it[3])
                    except: a = None
                if len(it) > 4:
                    try: b = int(it[4])
                    except: b = None
                out.append([code, flag, name, a, b])
        return out

    cities_from_json = normalize_cities_tree(cfg.get("CITIES") or cfg.get("cities") or {})
    cc_from_json     = _norm_cc(cfg.get("COUNTRY_CHOICES") or cfg.get("country_choices"))
    do_sync          = bool(cfg.get("SYNC_JSON_WITH_DB", False))

    conn = await get_connection()
    try:
        row = await conn.fetchrow(f"SELECT * FROM {CONFIG_TABLE} WHERE id=$1", CONFIG_ID)
        if not row:
            # нет записи — создаём, опираясь на config.json (если он есть)
            await conn.execute(
                f"""
                INSERT INTO {CONFIG_TABLE}
                    (id, cities, country_choices,
                     stars_enabled, check_country, region_in_bot,
                     recruitment_scan_intervel, recruitment_max_minutes,
                     updated_at)
                VALUES
                    ($1, $2::jsonb, $3::jsonb,
                     $4, $5, $6,
                     $7, $8,
                     now())
                """,
                CONFIG_ID,
                json.dumps(cities_from_json or {}, ensure_ascii=False),
                json.dumps(cc_from_json or [], ensure_ascii=False),
                bool(cfg.get("stars_enabled", False)),
                bool(cfg.get("check_country", False)),
                bool(cfg.get("region_in_bot", True)),
                int(cfg.get("recruitment_scan_intervel", 30) or 30),
                int(cfg.get("recruitment_max_minutes", 15) or 15),
            )
            _invalidate_cache()
        else:
            db = dict(row)

            # --- cities
            current_cities = _as_json_obj(db.get("cities"))
            normalized = _normalize_cities_tree_for_storage(current_cities)
            target_cities = None
            if do_sync:
                target_cities = cities_from_json
            elif normalized is None and cities_from_json:
                target_cities = cities_from_json
            elif normalized is not None:
                target_cities = normalized

            if target_cities is not None and not _json_equal(current_cities, target_cities):
                await conn.execute(
                    f"UPDATE {CONFIG_TABLE} SET cities=$1::jsonb, updated_at=now() WHERE id=$2",
                    json.dumps(target_cities, ensure_ascii=False),
                    CONFIG_ID,
                )
                _invalidate_cache()

            # --- country_choices
            def _norm_db_cc(obj):
                return _norm_cc(obj)  # приводим к списку списков

            current_cc = _as_json_obj(db.get("country_choices"))
            norm_db_cc = _norm_db_cc(current_cc)
            target_cc = None
            if do_sync:
                target_cc = cc_from_json
            elif not norm_db_cc and cc_from_json:
                target_cc = cc_from_json

            if target_cc is not None and not _json_equal(norm_db_cc, target_cc):
                await conn.execute(
                    f"UPDATE {CONFIG_TABLE} SET country_choices=$1::jsonb, updated_at=now() WHERE id=$2",
                    json.dumps(target_cc, ensure_ascii=False),
                    CONFIG_ID,
                )
                _invalidate_cache()

        # вернуть актуальную строку
        row = await conn.fetchrow(f"SELECT * FROM {CONFIG_TABLE} WHERE id=$1", CONFIG_ID)
        return dict(row or {})
    finally:
        await release_connection(conn)


# рядом с остальными утилитами
def _as_json_obj(val):
    """Если val — JSON-строка, распарсить в Python-объект, иначе вернуть как есть."""
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return val
    return val


def _coerce_cities_to_list(val) -> List[str]:
    """
    Унифицируем в ПЛОСКИЙ список названий городов (для UI).
    Поддерживает:
      - новый формат: {country: {region: [cities...]}}
      - старый формат: ["Hamburg", "Flensburg"]
      - строковые/объектные элементы
    """
    # Новый формат (дерево)
    if isinstance(val, dict):
        names: List[str] = []
        for _country, regions in val.items():
            if isinstance(regions, dict):
                for _region, cities in regions.items():
                    if isinstance(cities, list):
                        for c in cities:
                            nm = _pick_name(c)
                            if nm:
                                names.append(nm)
                    elif isinstance(cities, str):
                        nm = cities.strip()
                        if nm:
                            names.append(nm)
            elif isinstance(regions, list):
                for c in regions:
                    nm = _pick_name(c)
                    if nm:
                        names.append(nm)
            elif isinstance(regions, str):
                nm = regions.strip()
                if nm:
                    names.append(nm)
        return names

    # Старый формат: список
    if isinstance(val, list):
        names = []
        for it in val:
            nm = _pick_name(it)
            if nm:
                names.append(nm)
        return names

    # Строка → один город
    if isinstance(val, str):
        return [val.strip()] if val.strip() else []

    return []


def _json_equal(a: Any, b: Any) -> bool:
    """Сравнение JSON-структур по дампу (на случай разных типов/адаптеров)."""
    try:
        return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True, ensure_ascii=False)
    except Exception:
        return a == b


def _normalize_cities_tree_for_storage(val: Any) -> Optional[Dict[str, Dict[str, List[str]]]]:
    """
    Возвращает нормализованное дерево:
        country -> region -> [city, ...]
    или None, если нормализовать невозможно (тогда используем дефолт).
    Допускаем «упрощения» и приводим их:
      - country -> "Hamburg"        → country -> {"Hamburg": ["Hamburg"]}
      - country -> ["Hamburg", ...] → country -> {"<unknown>": [...]}  (не используем — лучше явные регионы)
      - region -> "City"            → region -> ["City"]
    """
    # Если уже «дерево» правильного типа
    if isinstance(val, dict):
        out: Dict[str, Dict[str, List[str]]] = {}
        for country, regions in val.items():
            if not isinstance(country, str) or not country.strip():
                return None
            country = country.strip()
            out.setdefault(country, {})
            if isinstance(regions, dict):
                # ожидаемый вариант: region -> (list|str)
                for region, cities in regions.items():
                    if not isinstance(region, str) or not region.strip():
                        return None
                    region = region.strip()
                    if isinstance(cities, list):
                        names = [_pick_name(c) for c in cities]
                        names = [n for n in names if n]
                        out[country][region] = names
                    elif isinstance(cities, str):
                        nm = cities.strip()
                        out[country][region] = [nm] if nm else []
                    else:
                        return None
            elif isinstance(regions, list):
                # под страной сразу список городов — поместим их в регион с именем страны
                names = [_pick_name(c) for c in regions]
                names = [n for n in names if n]
                out[country][country] = names
            elif isinstance(regions, str):
                nm = regions.strip()
                if nm:
                    out[country][nm] = [nm]
                else:
                    out[country][country] = []
            else:
                return None
        return out

    # Если пришёл список (старый формат like ["Hamburg", "Flensburg"])
    if isinstance(val, list):
        names = [_pick_name(c) for c in val]
        names = [n for n in names if n]
        if not names:
            return None
        # По задаче нам нужны конкретные привязки — сделаем минимальную валидную структуру.
        return CITIES

    # Если пришла строка — воспринимаем как «сломанный» формат
    if isinstance(val, str):
        nm = val.strip()
        if not nm:
            return None
        return CITIES

    # Иначе нормализация невозможна
    return None


async def get_city_names() -> list[str]:
    """
    Нормализованный список имён городов — всегда список строк.
    """
    row = await _fetch_config_row()
    raw = (row or {}).get("cities", [])
    lst = _coerce_cities_to_list(raw)
    names = []
    for it in lst:
        nm = _pick_name(it)
        if nm:
            names.append(nm)

    # Если пусто — залогируем, чтобы сразу увидеть «плохой» формат
    try:
        if not names:
            await log_info(f"get_city_names(): RAW type={type(raw).__name__} value={raw}", type_msg="warning")
    except Exception:
        pass

    return names

async def _fetch_config_row() -> Optional[Dict[str, Any]]:
    conn = None
    try:
        conn = await get_connection()
        row = await conn.fetchrow(
            f"""
            SELECT id, cities, stars_enabled, recruitment_scan_intervel,
                   check_country, region_in_bot,
                   recruitment_max_minutes, updated_at
              FROM {CONFIG_TABLE}
             WHERE id = $1
             LIMIT 1
            """,
            CONFIG_ID,
        )
        if not row:
            await log_info(f"{CONFIG_TABLE} id={CONFIG_ID} не найдена → создаю дефолт", type_msg="warning")
            await ensure_config_exists()
            row = await conn.fetchrow(
                f"""
                SELECT id, cities, stars_enabled, recruitment_scan_intervel,
                       check_country, region_in_bot,
                       recruitment_max_minutes, updated_at
                  FROM {CONFIG_TABLE}
                 WHERE id = $1
                 LIMIT 1
                """,
                CONFIG_ID,
            )
            if not row:
                return None
        return dict(row)
    except Exception as e:
        await log_info(f"_fetch_config_row failed: {e}", type_msg="error")
        return None
    finally:
        if conn:
            await release_connection(conn)


# ──────────────────────────────────────────────────────────────────────────────
# Публичные «геттеры». Каждый вызов = новое чтение из БД.
# ──────────────────────────────────────────────────────────────────────────────

async def get_cities() -> List[str]:
    """
    Возвращает ПЛОСКИЙ список имён городов независимо от формата хранения.
    """
    row = await _fetch_config_row()
    raw = (row or {}).get("cities", [])
    names = _coerce_cities_to_list(raw)

    # Если пусто — залогируем, чтобы увидеть «плохой» формат
    try:
        if not names:
            await log_info(f"get_cities(): RAW type={type(raw).__name__} value={raw}", type_msg="warning")
    except Exception:
        pass

    return names


async def get_stars_enabled() -> bool:
    """
    Вернёт признак включения Telegram Stars.
    По схеме: stars_enabled BOOLEAN NOT NULL DEFAULT FALSE
    """
    row = await _fetch_config_row()
    return bool((row or {}).get("stars_enabled", False))


async def get_recruitment_scan_intervel() -> int:
    """
    Вернёт интервал сканирования (сек.).
    Имя сохранено с исходной опечаткой: recruitment_scan_intervel.
    По схеме: INTEGER NOT NULL DEFAULT 30 CHECK (recruitment_scan_intervel > 0)
    """
    row = await _fetch_config_row()
    try:
        return int((row or {}).get("recruitment_scan_intervel", 30))
    except Exception:
        return 30


async def get_recruitment_scan_interval() -> int:
    """
    Алиас с «правильным» написанием имени.
    """
    return await get_recruitment_scan_intervel()


async def get_recruitment_max_minutes() -> int:
    """
    Вернёт максимум подбора (мин.).
    По схеме: INTEGER NOT NULL DEFAULT 15 CHECK (recruitment_max_minutes > 0)
    """
    row = await _fetch_config_row()
    try:
        return int((row or {}).get("recruitment_max_minutes", 15))
    except Exception:
        return 15


async def get_updated_at():
    """
    Вернёт updated_at (TIMESTAMPTZ) как есть (обычно datetime с tzinfo) или None.
    """
    row = await _fetch_config_row()
    return (row or {}).get("updated_at")


async def get_all_config() -> Dict[str, Any]:
    row = await _fetch_config_row() or {}
    cities_val = _as_json_obj(row.get("cities", {}))
    return {
        "id": row.get("id", CONFIG_ID),
        "cities": cities_val if isinstance(cities_val, dict) else {},
        "stars_enabled": bool(row.get("stars_enabled", False)),
        "check_country": bool(row.get("check_country", False)),    # ← новое
        "region_in_bot": bool(row.get("region_in_bot", True)),     # ← новое
        "recruitment_scan_intervel": int(row.get("recruitment_scan_intervel", 30) or 30),
        "recruitment_max_minutes": int(row.get("recruitment_max_minutes", 15) or 15),
        "updated_at": row.get("updated_at"),
    }


# === ВЫБОРКИ ДЛЯ КЛАВИАТУР ====================================================

async def list_countries() -> List[str]:
    data = (await _get_config_cached()).get("cities")
    if isinstance(data, str):
        try: data = json.loads(data)
        except Exception: data = {}
    tree = normalize_cities_tree(data)
    return sorted(tree.keys(), key=str.casefold)


async def list_regions(country: str) -> List[str]:
    tree = normalize_cities_tree((await _get_config_cached()).get("cities") or {})
    regions = tree.get((country or "").strip()) or {}
    return sorted(regions.keys(), key=str.casefold)


async def list_cities(country: str, region: str) -> List[str]:
    tree = normalize_cities_tree((await _get_config_cached()).get("cities") or {})
    arr = (tree.get((country or "").strip()) or {}).get((region or "").strip()) or []
    return arr  # уже отсортировано и без дублей


# === JSONB: страны / земли (регионы) =====================================================

async def _upsert_country_sql(country: str) -> dict:
    country = country.strip()
    if not country:
        return {}
    conn = await get_connection()
    try:
        row = await conn.fetchrow(
            f"""
            WITH base AS (
              SELECT COALESCE(
                       CASE WHEN jsonb_typeof(cities)='object' THEN cities ELSE '{{}}'::jsonb END,
                       '{{}}'::jsonb
                     ) AS obj
              FROM {CONFIG_TABLE}
              WHERE id=$1
            )
            UPDATE {CONFIG_TABLE}
               SET cities = jsonb_set(
                     (SELECT obj FROM base),
                     ARRAY[$2]::text[],
                     '{{}}'::jsonb,
                     true
                   ),
                   updated_at = now()
             WHERE id = $1
         RETURNING cities, updated_at
            """,
            CONFIG_ID, country,
        )
        _invalidate_cache()  # важно
        return dict(row or {})
    finally:
        await release_connection(conn)


async def _upsert_city_sql(country: str, region: str, city: str) -> dict:
    """
    Вставляет city в конец {country,region} без дублей (case-insensitive).
    Если пути нет — создаёт. Возвращает обновлённые cities, updated_at.
    """
    country, region, city = country.strip(), region.strip(), city.strip()
    if not (country and region and city):
        return {}
    conn = await get_connection()
    try:
        row = await conn.fetchrow(
            f"""
            WITH base AS (
              SELECT
                COALESCE(
                  CASE WHEN jsonb_typeof(cities)='object' THEN cities ELSE '{{}}'::jsonb END,
                  '{{}}'::jsonb
                ) AS obj
              FROM {CONFIG_TABLE}
              WHERE id=$1
            ),
            cur AS (
              SELECT COALESCE((obj -> $2 -> $3), '[]'::jsonb) AS arr
              FROM base
            ),
            new_arr AS (
              SELECT CASE
                WHEN EXISTS(
                   SELECT 1
                     FROM jsonb_array_elements_text((SELECT arr FROM cur)) v
                    WHERE lower(v.value)=lower($4)
                )
                THEN (SELECT arr FROM cur)
                ELSE (SELECT arr FROM cur) || to_jsonb($4::text)
              END AS value
            )
            UPDATE {CONFIG_TABLE}
               SET cities = jsonb_set(
                    (SELECT obj FROM base),
                    ARRAY[$2,$3]::text[],
                    (SELECT value FROM new_arr),
                    true
               ),
                   updated_at = now()
             WHERE id=$1
         RETURNING cities, updated_at
            """,
            CONFIG_ID, country, region, city,
        )
        return dict(row or {})
    finally:
        await release_connection(conn)


async def _remove_city_sql_tree(country: str, region: str, city: str) -> dict:
    """
    Удаляет city из массива {country,region}. Если пути нет — noop.
    """
    country, region, city = country.strip(), region.strip(), city.strip()
    if not (country and region and city):
        return {}
    conn = await get_connection()
    try:
        row = await conn.fetchrow(
            f"""
            WITH base AS (
              SELECT
                COALESCE(
                  CASE WHEN jsonb_typeof(cities)='object' THEN cities ELSE '{{}}'::jsonb END,
                  '{{}}'::jsonb
                ) AS obj
              FROM {CONFIG_TABLE}
              WHERE id=$1
            ),
            cur AS (
              SELECT COALESCE((obj -> $2 -> $3), '[]'::jsonb) AS arr
              FROM base
            ),
            filtered AS (
              WITH elems AS (
                SELECT e.elem, e.ord
                FROM jsonb_array_elements((SELECT arr FROM cur)) WITH ORDINALITY AS e(elem, ord)
              )
              SELECT COALESCE(
                (
                  SELECT jsonb_agg(elem ORDER BY ord)
                  FILTER (WHERE NOT (
                    (jsonb_typeof(elem)='string'
                         AND lower(trim(BOTH '"' FROM elem::text)) = lower($4))
                    OR
                    (jsonb_typeof(elem)='object'
                         AND lower(COALESCE(elem->>'name', elem->>'title', elem->>'city',''))
                             = lower($4))
                  ))
                  FROM elems
                ),
                '[]'::jsonb
              ) AS value
            )
            UPDATE {CONFIG_TABLE}
               SET cities = jsonb_set(
                    (SELECT obj FROM base),
                    ARRAY[$2,$3]::text[],
                    (SELECT value FROM filtered),
                    true
               ),
                   updated_at = now()
             WHERE id=$1
         RETURNING cities, updated_at
            """,
            CONFIG_ID, country, region, city,
        )
        return dict(row or {})
    finally:
        await release_connection(conn)


async def _remove_country_sql(country: str) -> dict:
    country = country.strip()
    if not country:
        return {}
    conn = await get_connection()
    try:
        row = await conn.fetchrow(
            f"""
            UPDATE {CONFIG_TABLE}
               SET cities = CASE
                   WHEN jsonb_typeof(COALESCE(cities, '{{}}'::jsonb)) = 'object'
                   THEN COALESCE(cities, '{{}}'::jsonb) - $2
                   ELSE '{{}}'::jsonb
               END,
                   updated_at = now()
             WHERE id = $1
         RETURNING cities, updated_at
            """,
            CONFIG_ID, country,
        )
        return dict(row or {})
    finally:
        await release_connection(conn)


async def _upsert_region_sql(country: str, region: str) -> dict:
    country, region = country.strip(), region.strip()
    if not country or not region:
        return {}
    conn = await get_connection()
    try:
        row = await conn.fetchrow(
            f"""
            WITH base AS (
              SELECT COALESCE(
                       CASE WHEN jsonb_typeof(cities)='object' THEN cities ELSE '{{}}'::jsonb END,
                       '{{}}'::jsonb
                     ) AS obj
              FROM {CONFIG_TABLE}
              WHERE id=$1
            )
            UPDATE {CONFIG_TABLE}
               SET cities = jsonb_set(
                     (SELECT obj FROM base),
                     ARRAY[$2,$3]::text[],
                     '[]'::jsonb,
                     true
                   ),
                   updated_at = now()
             WHERE id = $1
         RETURNING cities, updated_at
            """,
            CONFIG_ID, country, region,
        )
        _invalidate_cache()  # важно
        return dict(row or {})
    finally:
        await release_connection(conn)


async def _remove_region_sql(country: str, region: str) -> dict:
    country, region = country.strip(), region.strip()
    if not country or not region:
        return {}
    conn = await get_connection()
    try:
        row = await conn.fetchrow(
            f"""
            WITH base AS (
              SELECT COALESCE(
                       CASE WHEN jsonb_typeof(cities)='object' THEN cities ELSE '{{}}'::jsonb END,
                       '{{}}'::jsonb
                     ) AS obj
              FROM {CONFIG_TABLE}
              WHERE id=$1
            ),
            new_country AS (
              SELECT (obj -> $2)::jsonb - $3 AS value FROM base
            )
            UPDATE {CONFIG_TABLE}
               SET cities = jsonb_set(
                     (SELECT obj FROM base),
                     ARRAY[$2]::text[],
                     COALESCE((SELECT value FROM new_country), '{{}}'::jsonb),
                     false
                   ),
                   updated_at = now()
             WHERE id = $1
         RETURNING cities, updated_at
            """,
            CONFIG_ID, country, region,
        )
        _invalidate_cache()  # важно
        return dict(row or {})
    finally:
        await release_connection(conn)


async def read_cities_json() -> Dict[str, Any]:
    """Читает колонку config.cities (id=1) и возвращает Python-объект или {} при ошибке."""
    conn = None
    try:
        conn = await get_connection()
        row = await conn.fetchrow(f"SELECT cities FROM {CONFIG_TABLE} WHERE id=$1 LIMIT 1", CONFIG_ID)
        if not row:
            await log_info("read_cities_json(): строка config id=1 не найдена", type_msg="warning")
            return {}
        val = row["cities"]
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception as e:
                await log_info(f"read_cities_json(): не удалось распарсить JSON: {e}", type_msg="warning")
                return {}
        return val if isinstance(val, (dict, list)) else {}
    except Exception as e:
        await log_info(f"read_cities_json() failed: {e}", type_msg="error")
        return {}
    finally:
        if conn:
            await release_connection(conn)

def _pick_name(x: Any) -> str:
    if isinstance(x, str):
        return x.strip()
    if isinstance(x, dict):
        for k in ("name", "title", "city", "label", "text", "value"):
            v = x.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""

async def get_countries_regions_cities_lists_aligned() -> Tuple[List[str], List[str], List[List[str]]]:
    """
    Возвращает три списка с выравниванием регионов и городов:
      countries -> ["Germany", ...]                         (уникальные страны, отсортированы)
      regions   -> ["Hamburg", "Schleswig-Holstein", ...]   (по всем странам, в порядке стран)
      cities    -> [["Hamburg"], ["Flensburg","Husum","Kiel"], ...]  (i-й список -> i-й регион)
    Правила нормализации:
      - ожидаемый формат: {country: {region: [cities...]}}
      - допускается {country: [cities...]}  -> регион = название страны
      - допускается старый формат: [city,...] -> страна=region="Default"
      - в каждом списке значения уникализируются и сортируются case-insensitive
    """
    raw = await read_cities_json()

    countries: List[str] = []
    regions:   List[str] = []
    cities:    List[List[str]] = []

    if isinstance(raw, dict):
        # Сортируем страны
        for country in sorted((k for k in raw.keys() if isinstance(k, str) and k.strip()), key=str.casefold):
            c = country.strip()
            countries.append(c)

            value = raw[country]
            # Случай: под страной словарь регионов
            if isinstance(value, dict):
                # Сортируем регионы
                for region in sorted((r for r in value.keys() if isinstance(r, str) and r.strip()), key=str.casefold):
                    r = region.strip()
                    # Города региона
                    arr: List[str] = []
                    cities_val = value[region]
                    if isinstance(cities_val, list):
                        arr = [_pick_name(it) for it in cities_val]
                    elif isinstance(cities_val, str):
                        arr = [cities_val.strip()]
                    arr = sorted(set(x for x in arr if x), key=str.casefold)

                    regions.append(r)
                    cities.append(arr)

            # Случай: под страной сразу список городов → регион = имя страны
            elif isinstance(value, list):
                arr = [_pick_name(it) for it in value]
                arr = sorted(set(x for x in arr if x), key=str.casefold)

                regions.append(c)   # регион = страна
                cities.append(arr)

            # Случай: под страной одна строка (регион без явных городов)
            elif isinstance(value, str):
                r = value.strip() or c
                regions.append(r)
                cities.append([])

    elif isinstance(raw, list):
        # Старый формат: список городов → одна виртуальная страна/регион
        c = "Default"
        countries.append(c)
        regions.append(c)
        arr = [_pick_name(it) for it in raw]
        cities.append(sorted(set(x for x in arr if x), key=str.casefold))

    # Убираем дубликаты стран, сохраняя порядок (если данные странно задублированы)
    seen = set()
    countries = [x for x in countries if not (x in seen or seen.add(x))]

    return countries, regions, cities


async def load_cities() -> Dict[str, Dict[str, List[str]]]:
    row = await _get_config_cached()
    raw = row.get("cities")
    if isinstance(raw, str):
        try: raw = json.loads(raw)
        except Exception: raw = None
    norm = normalize_cities_tree(raw)
    if norm:
        return norm
    cfg = get_settings("config/config.json") or {}
    return normalize_cities_tree(cfg.get("CITIES") or cfg.get("cities") or {})


async def load_country_choices() -> List[Tuple[str, str, str, Optional[int], Optional[int]]]:
    # 1) БД
    conn = await get_connection()
    try:
        row = await conn.fetchrow('SELECT country_choices FROM "config" WHERE id=1;')
        if not row:
            row = await conn.fetchrow('SELECT country_choices FROM "config" ORDER BY id LIMIT 1;')
        raw = row['country_choices'] if row else None
    finally:
        await release_connection(conn)

    if isinstance(raw, str):
        try: raw = json.loads(raw)
        except Exception: raw = None

    def _norm(obj):
        out = []
        if isinstance(obj, (list, tuple)):
            for it in obj:
                if not isinstance(it, (list, tuple)) or len(it) < 3:
                    continue
                code = str(it[0]).strip()
                flag = str(it[1]).strip()
                name = str(it[2]).strip()
                a = b = None
                if len(it) > 3:
                    try: a = int(it[3])
                    except: a = None
                if len(it) > 4:
                    try: b = int(it[4])
                    except: b = None
                out.append((code, flag, name, a, b))
        return out

    norm = _norm(raw)
    if norm:
        return norm

    # 2) Фолбэк — файл
    cfg = get_settings("config/config.json") or {}
    return _norm(cfg.get('COUNTRY_CHOICES') or cfg.get('country_choices'))
