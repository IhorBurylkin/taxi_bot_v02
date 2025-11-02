from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
from typing import Any, TypedDict
from uuid import uuid4

from aiohttp import ClientError, ClientSession, ClientTimeout
from nicegui import app, ui
from yarl import URL

from config.config import GMAPS_API_KEY, GMAPS_URL_SIGNING_SECRET, USERS_TABLE
from config.config_utils import lang_dict
from db.db_utils import get_user_data
from log.log import log_info

__all__ = ["render_main_map"]

GMAPS_HOST = "maps.googleapis.com"
GEOCODE_PATH = "/maps/api/geocode/json"

SCRIPT_FLAG_KEY = "main_map_gmaps_script_loaded"
HANDLER_FLAG_KEY = "main_map_geo_handler_registered"
GEO_NOTIFIED_CODES_KEY = "main_map_geo_notified_codes"
FALLBACK_CACHE_KEY = "main_map_geocode_cache"
FALLBACK_LOGGED_KEY = "main_map_fallback_logged"
FALLBACK_FAILED_LOGGED_KEY = "main_map_fallback_failed_logged"
FALLBACK_NOTIFIED_KEY = "main_map_fallback_notified"
FALLBACK_FAILED_NOTIFIED_KEY = "main_map_fallback_failed_notified"

MAP_INIT_TIMEOUT_SEC = 12.0
GEO_PERMISSION_TIMEOUT_SEC = 3.0
GEO_MAXIMUM_AGE_MS = 1000
GEO_TIMEOUT_MS = 10000
DEFAULT_INITIAL_ZOOM = 16
DEFAULT_FALLBACK_ZOOM = 12

USERS_TABLE_NAME = USERS_TABLE or "users"


class FallbackLocation(TypedDict):
	lat: float
	lng: float
	address: str


def _set_client_value(key: str, value: Any) -> None:
	app.storage.client[key] = value


def _get_client_value(key: str, default: Any = None) -> Any:
	return app.storage.client.get(key, default)


def _get_api_key() -> str | None:
	env_key = os.getenv("GMAPS_API_KEY")
	if env_key:
		return env_key
	return GMAPS_API_KEY


async def _append_signature(
	path: str,
	params: list[tuple[str, str]],
	uid: int | None,
) -> list[tuple[str, str]]:
	if not GMAPS_URL_SIGNING_SECRET:
		return params

	try:
		padded = GMAPS_URL_SIGNING_SECRET + "=" * (-len(GMAPS_URL_SIGNING_SECRET) % 4)
		key = base64.urlsafe_b64decode(padded.encode("utf-8"))
		url = URL.build(scheme="https", host=GMAPS_HOST, path=path, query=params)
		resource = url.raw_path.encode("utf-8")
		digest = hmac.new(key, resource, hashlib.sha1).digest()
		signature = base64.urlsafe_b64encode(digest).decode("utf-8")
		return [*params, ("signature", signature)]
	except Exception as sign_error:  # noqa: BLE001
		await log_info(
			"[main_map] не удалось подписать запрос Google Maps",
			type_msg="warning",
			uid=uid,
			reason=str(sign_error),
		)
		return params


async def _log_fallback_usage(
	fallback: FallbackLocation, uid: int | None,
) -> None:
	if _get_client_value(FALLBACK_LOGGED_KEY):
		return

	await log_info(
		"[main_map] применены координаты стартовой точки из профиля",
		type_msg="info",
		uid=uid,
		address=fallback.get("address"),
		lat=fallback.get("lat"),
		lng=fallback.get("lng"),
	)
	_set_client_value(FALLBACK_LOGGED_KEY, True)


async def _log_fallback_absence(uid: int | None) -> None:
	if _get_client_value(FALLBACK_FAILED_LOGGED_KEY):
		return

	await log_info(
		"[main_map] координаты стартовой точки недоступны",
		type_msg="warning",
		uid=uid,
	)
	_set_client_value(FALLBACK_FAILED_LOGGED_KEY, True)


async def _resolve_fallback_coordinates(
	uid: int | None,
	safe_lang: str,
	user_data: dict | None,
	api_key: str,
) -> FallbackLocation | None:
	cached: FallbackLocation | None = _get_client_value("main_map_last_fallback")
	if cached:
		return cached

	resolved_user = user_data
	if resolved_user is None and uid is not None:
		try:
			resolved_user = await get_user_data(USERS_TABLE_NAME, uid)
		except Exception as db_error:  # noqa: BLE001
			await log_info(
				"[main_map] ошибка получения профиля из БД",
				type_msg="error",
				uid=uid,
				reason=str(db_error),
			)

	if not resolved_user:
		return None

	parts: list[str] = []
	for key in ("city", "region", "country"):
		value = resolved_user.get(key)
		if isinstance(value, str) and value.strip():
			parts.append(value.strip())

	if not parts:
		await log_info(
			"[main_map] отсутствуют адресные данные для стартовой точки",
			type_msg="warning",
			uid=uid,
		)
		return None

	address = ", ".join(parts)
	cache: dict[str, FallbackLocation] = _get_client_value(FALLBACK_CACHE_KEY, {})
	cache_key = f"{address}|{safe_lang}"
	if cache_key in cache:
		fallback = cache[cache_key]
		_set_client_value("main_map_last_fallback", fallback)
		return fallback

	params: list[tuple[str, str]] = [
		("address", address),
		("key", api_key),
		("language", safe_lang.lower()),
	]
	signed_params = await _append_signature(GEOCODE_PATH, params, uid)

	try:
		timeout = ClientTimeout(total=8)
		async with ClientSession(timeout=timeout) as session:
			async with session.get(
				f"https://{GMAPS_HOST}{GEOCODE_PATH}", params=signed_params
			) as response:
				payload: dict[str, Any] = await response.json(content_type=None)
	except ClientError as http_error:
		await log_info(
			"[main_map] ошибка HTTP при запросе геокодера",
			type_msg="warning",
			uid=uid,
			reason=str(http_error),
		)
		return None
	except Exception as unexpected_error:  # noqa: BLE001
		await log_info(
			"[main_map] исключение при запросе геокодера",
			type_msg="error",
			uid=uid,
			reason=str(unexpected_error),
		)
		return None

	status = str(payload.get("status") or "").upper()
	if status != "OK":
		await log_info(
			"[main_map] геокодер вернул пустой статус",
			type_msg="warning",
			uid=uid,
			status=status,
			address=address,
		)
		return None

	results = payload.get("results") or []
	if not results:
		await log_info(
			"[main_map] геокодер не нашёл координаты",
			type_msg="warning",
			uid=uid,
			address=address,
		)
		return None

	primary = results[0]
	geometry = primary.get("geometry") or {}
	location = geometry.get("location") or {}
	lat = location.get("lat")
	lng = location.get("lng")
	if lat is None or lng is None:
		await log_info(
			"[main_map] геокодер вернул ответ без координат",
			type_msg="warning",
			uid=uid,
			address=address,
		)
		return None

	fallback: FallbackLocation = {
		"lat": float(lat),
		"lng": float(lng),
		"address": primary.get("formatted_address") or address,
	}
	cache[cache_key] = fallback
	_set_client_value(FALLBACK_CACHE_KEY, cache)
	_set_client_value("main_map_last_fallback", fallback)

	await log_info(
		"[main_map] получены координаты стартовой точки из профиля",
		type_msg="info",
		uid=uid,
		address=fallback.get("address"),
		lat=fallback.get("lat"),
		lng=fallback.get("lng"),
	)
	return fallback


async def _notify_geo_issue(
	issue_code: Any,
	fallback: FallbackLocation | None,
	safe_lang: str,
	uid: int | None,
) -> None:
	code_str = str(issue_code)
	notified: list[str] = list(_get_client_value(GEO_NOTIFIED_CODES_KEY, []))
	if code_str in notified:
		return

	if code_str in {"1", "denied"}:
		message_key = "map_notify_geolocation_denied"
	elif code_str in {"unsupported"}:
		message_key = "map_notify_geolocation_unsupported"
	elif code_str in {"2", "position_unavailable"}:
		message_key = "map_notify_geolocation_unavailable"
	elif code_str in {"3", "timeout", str(GEO_TIMEOUT_MS)}:
		message_key = "map_notify_geolocation_timeout"
	elif code_str in {"gmaps-timeout"}:
		message_key = "map_notify_unexpected_error"
	else:
		message_key = "map_notify_geolocation_unavailable"

	ui.notify(lang_dict(message_key, safe_lang), type="warning")

	if code_str in {"1", "denied", "unsupported"}:
		if fallback:
			if not _get_client_value(FALLBACK_NOTIFIED_KEY):
				ui.notify(
					lang_dict(
						"map_notify_fallback_location",
						safe_lang,
						address=fallback.get("address"),
					),
					type="warning",
				)
				await _log_fallback_usage(fallback, uid)
				_set_client_value(FALLBACK_NOTIFIED_KEY, True)
		else:
			if not _get_client_value(FALLBACK_FAILED_NOTIFIED_KEY):
				ui.notify(
					lang_dict("map_notify_fallback_failed", safe_lang),
					type="warning",
				)
				await _log_fallback_absence(uid)
				_set_client_value(FALLBACK_FAILED_NOTIFIED_KEY, True)

	notified.append(code_str)
	_set_client_value(GEO_NOTIFIED_CODES_KEY, notified)


async def _ensure_geo_error_handler() -> None:
	if _get_client_value(HANDLER_FLAG_KEY):
		return

	async def _handle_geo_error(event) -> None:  # noqa: ANN001
		current_uid = _get_client_value("main_map_geo_uid")
		safe_lang = _get_client_value("main_map_geo_lang", "en")
		fallback: FallbackLocation | None = _get_client_value("main_map_last_fallback")

		detail: dict[str, Any] = {}
		if isinstance(event.args, list) and event.args:
			maybe_dict = event.args[0]
			if isinstance(maybe_dict, dict):
				detail = maybe_dict
			else:
				detail = {"code": maybe_dict}
		elif isinstance(event.args, dict):
			detail = event.args

		code = detail.get("code", "unknown")

		try:
			await log_info(
				"[main_map] получен сигнал об ошибке геолокации",
				type_msg="warning",
				uid=current_uid,
				geo_code=code,
			)
			await _notify_geo_issue(code, fallback, safe_lang, current_uid)
		except Exception as handler_error:  # noqa: BLE001
			await log_info(
				"[main_map] ошибка обработчика события геолокации",
				type_msg="error",
				uid=current_uid,
				reason=str(handler_error),
			)

	ui.on("main_map_geo_error", _handle_geo_error)
	_set_client_value(HANDLER_FLAG_KEY, True)


async def _check_geolocation_permission(uid: int | None) -> str:
	try:
		result = await ui.run_javascript(  # type: ignore[arg-type]
			"""
			(async () => {
			  if (!navigator?.geolocation) {
				return { status: 'unsupported' };
			  }
			  if (navigator.permissions?.query) {
				try {
				  const permission = await navigator.permissions.query({ name: 'geolocation' });
				  return { status: permission?.state ?? 'unknown' };
				} catch (permError) {
				  return { status: 'unknown', message: String(permError?.message ?? '') };
				}
			  }
			  return { status: 'unknown' };
			})();
			""",
			timeout=GEO_PERMISSION_TIMEOUT_SEC,
		)
		status = str((result or {}).get("status") or "unknown").lower()
		await log_info(
			"[main_map] статус разрешения геолокации",
			type_msg="info",
			uid=uid,
			status=status,
		)
		return status
	except asyncio.TimeoutError as timeout_error:
		await log_info(
			"[main_map] таймаут проверки разрешения геолокации",
			type_msg="warning",
			uid=uid,
			reason=str(timeout_error),
		)
		return "timeout"
	except Exception as permission_error:  # noqa: BLE001
		await log_info(
			"[main_map] ошибка проверки разрешения геолокации",
			type_msg="error",
			uid=uid,
			reason=str(permission_error),
		)
		return "error"


async def render_main_map(
	uid: int | None,
	user_lang: str,
	user_data: dict | None,
) -> None:
	"""Рендерит полноэкранную карту Google Maps с отслеживанием позиции."""

	safe_lang = user_lang or "en"
	_set_client_value("main_map_geo_lang", safe_lang)
	if uid is not None:
		_set_client_value("main_map_geo_uid", uid)

	try:
		await log_info(
			"[main_map] старт отрисовки карты",
			type_msg="info",
			uid=uid,
		)

		await _ensure_geo_error_handler()

		api_key = _get_api_key()
		if not api_key:
			await log_info(
				"[main_map] отсутствует ключ Google Maps",
				type_msg="warning",
				uid=uid,
			)
			ui.notify(lang_dict("map_notify_gmaps_missing_key", safe_lang), type="warning")
			with ui.column().classes("w-full items-center justify-center gap-2 q-pa-lg"):
				ui.icon("map").classes("text-4xl text-negative")
				ui.label(lang_dict("map_message_missing_api_key", safe_lang)).classes(
					"text-negative text-center"
				)
			return

		fallback = await _resolve_fallback_coordinates(uid, safe_lang, user_data, api_key)
		_set_client_value("main_map_last_fallback", fallback)

		if not _get_client_value(SCRIPT_FLAG_KEY):
			language_param = safe_lang.lower()
			script_url = (
				f"https://{GMAPS_HOST}/maps/api/js?key={api_key}&v=weekly"
				f"&libraries=geometry&loading=async&language={language_param}"
			)
			ui.add_head_html(
				f'<script src="{script_url}" data-taxibot-gmaps="1" async defer></script>'
			)
			_set_client_value(SCRIPT_FLAG_KEY, True)

		# Контейнер карты заранее резервирует доступную высоту.
		map_wrapper = ui.element("div").classes(
			"w-full q-pa-none q-ma-none flex-1 relative overflow-hidden"
		)
		map_wrapper.style(
			"min-height: calc(var(--main-app-viewport, 100vh) - var(--main-footer-height, 0px));"
			"height: calc(var(--main-app-viewport, 100vh) - var(--main-footer-height, 0px));"
			"width: 100%;"
		)
		with map_wrapper:
			container_id = f"main-map-{uuid4().hex}"
			map_canvas = ui.element("div").classes("w-full h-full")
			map_canvas.props(f"id={container_id}")
			map_canvas.style("width: 100%; height: 100%;")

		permission_status = await _check_geolocation_permission(uid)
		if permission_status in {"denied", "unsupported", "timeout"}:
			await _notify_geo_issue(permission_status, fallback, safe_lang, uid)

		init_payload = {
			"containerId": container_id,
			"fallback": {
				"lat": fallback["lat"],
				"lng": fallback["lng"],
			}
			if fallback
			else None,
			"markerTitle": lang_dict("map_marker_user", safe_lang),
			"initialZoom": DEFAULT_INITIAL_ZOOM,
			"fallbackZoom": DEFAULT_FALLBACK_ZOOM,
			"maxAgeMs": GEO_MAXIMUM_AGE_MS,
			"timeoutMs": GEO_TIMEOUT_MS,
		}

		js_code = f"""
		(async () => {{
			const opts = {json.dumps(init_payload)};
			const waitForMaps = () => new Promise((resolve, reject) => {{
				const started = Date.now();
				const check = () => {{
					if (window.google?.maps) {{
						resolve(window.google.maps);
						return;
					}}
					if (Date.now() - started > {int(MAP_INIT_TIMEOUT_SEC * 1000)}) {{
						reject(new Error('gmaps-timeout'));
						return;
					}}
					requestAnimationFrame(check);
				}};
				check();
			}});
			try {{
				await waitForMaps();
			}} catch (err) {{
				if (typeof emitEvent === 'function') {{
					emitEvent('main_map_geo_error', {{ code: 'gmaps-timeout', message: err?.message ?? '' }});
				}}
				return {{ status: 'gmaps-timeout' }};
			}}
			const container = document.getElementById(opts.containerId);
			if (!container) {{
				return {{ status: 'container-missing' }};
			}}
			container.innerHTML = '';
			if (window.__taxibot_map_state?.watchId != null && navigator.geolocation) {{
				try {{ navigator.geolocation.clearWatch(window.__taxibot_map_state.watchId); }} catch (_err) {{}}
			}}
			const maps = window.google.maps;
			const initialCenter = opts.fallback ?? {{ lat: 0, lng: 0 }};
			const map = new maps.Map(container, {{
				center: initialCenter,
				zoom: opts.fallback ? opts.fallbackZoom : 3,
				gestureHandling: 'greedy',
				streetViewControl: false,
				mapTypeControl: false,
				fullscreenControl: false,
				zoomControl: true,
			}});
			const marker = new maps.Marker({{
				position: initialCenter,
				map,
				title: opts.markerTitle || '',
				optimized: true,
			}});
			const state = {{
				map,
				marker,
				lastUpdate: null,
				watchId: null,
				containerId: opts.containerId,
			}};
			const updateMarker = (coords) => {{
				if (!coords) return;
				const nextPos = new maps.LatLng(coords.latitude, coords.longitude);
				marker.setPosition(nextPos);
				if (!state.lastUpdate) {{
					map.setZoom(opts.initialZoom ?? {DEFAULT_INITIAL_ZOOM});
					map.panTo(nextPos);
				}}
				state.lastUpdate = Date.now();
			}};
			if (opts.fallback) {{
				const fallbackPos = new maps.LatLng(opts.fallback.lat, opts.fallback.lng);
				map.panTo(fallbackPos);
			}}
			if (navigator.geolocation) {{
				const watchId = navigator.geolocation.watchPosition(
					(position) => {{
						updateMarker(position.coords);
					}},
					(error) => {{
						if (typeof emitEvent === 'function') {{
							emitEvent('main_map_geo_error', {{ code: error?.code ?? 'unknown', message: error?.message ?? '' }});
						}}
					}},
					{{
						enableHighAccuracy: true,
						maximumAge: opts.maxAgeMs ?? {GEO_MAXIMUM_AGE_MS},
						timeout: opts.timeoutMs ?? {GEO_TIMEOUT_MS},
					}}
				);
				state.watchId = watchId;
			}} else {{
				if (typeof emitEvent === 'function') {{
					emitEvent('main_map_geo_error', {{ code: 'unsupported', message: 'Geolocation API missing' }});
				}}
			}}
			window.__taxibot_map_state = state;
			return {{ status: 'ok' }};
		}})();
		"""

		try:
			init_result = await ui.run_javascript(js_code, timeout=MAP_INIT_TIMEOUT_SEC)
		except asyncio.TimeoutError as timeout_error:
			await log_info(
				"[main_map] таймаут инициализации карты",
				type_msg="warning",
				uid=uid,
				reason=str(timeout_error),
			)
			await _notify_geo_issue("gmaps-timeout", fallback, safe_lang, uid)
			ui.notify(lang_dict("map_notify_unexpected_error", safe_lang), type="warning")
			return
		except Exception as js_error:  # noqa: BLE001
			await log_info(
				"[main_map] ошибка исполнения JS карты",
				type_msg="error",
				uid=uid,
				reason=str(js_error),
			)
			ui.notify(lang_dict("map_notify_unexpected_error", safe_lang), type="error")
			return

		if isinstance(init_result, dict):
			status = init_result.get("status")
			if status != "ok":
				await log_info(
					"[main_map] карта вернула код завершения",
					type_msg="warning",
					uid=uid,
					status=status,
				)
				await _notify_geo_issue(status, fallback, safe_lang, uid)
				if status == "gmaps-timeout":
					ui.notify(lang_dict("map_notify_unexpected_error", safe_lang), type="warning")
				return

		await log_info(
			"[main_map] карта готова к работе",
			type_msg="info",
			uid=uid,
			fallback=bool(fallback),
		)

	except Exception as render_error:  # noqa: BLE001
		await log_info(
			"[main_map] общая ошибка рендера карты",
			type_msg="error",
			uid=uid,
			reason=str(render_error),
		)
		ui.notify(lang_dict("map_notify_unexpected_error", safe_lang), type="error")
