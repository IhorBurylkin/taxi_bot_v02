from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from nicegui import app, ui

from config.config_utils import lang_dict
from log.log import log_info
from web.web_utilits import TelegramBackButton, _safe_js
from db.db_utils import (
    cancel_order,
    complete_order,
    get_active_order_for_driver,
    get_driver_stats_summary,
    get_latest_open_order_id_for_passenger,
    get_order_data,
    list_available_orders_for_driver,
    list_future_orders_for_passenger,
    list_order_history_for_user,
    mark_driver_arrived,
    mark_passenger_comeout,
    mark_trip_started,
    reserve_order,
)

__all__ = ["render_order_tab"]

POLL_INTERVAL_SEC = 15.0


@dataclass
class ActiveOrderState:
    """Состояние активного заказа для отображения."""

    order: dict[str, Any] | None = None
    role: str = "passenger"
    awaiting_fee_deadline: datetime | None = None
    need_topup: bool = False


class MainButtonController:
    """Управление Telegram Main Button через безопасный JS."""

    def __init__(self, *, lang: str, client: Any) -> None:
        self._lang = lang
        self._client = client
        self._current_key: str | None = None
        self._is_visible: bool = False
        self._is_enabled: bool = False
        self._action: Callable[[], Awaitable[None]] | None = None
        self._bound: bool = False

    async def bind_handler(self, *, event_name: str, handler: Callable[[], Awaitable[None]]) -> None:
        """Регистрируем обработчик клика по главной кнопке."""

        self._action = handler
        if self._bound:
            return

        async def _emit_bound() -> None:
            try:
                await _safe_js(
                    """
                    (() => {
                        const webApp = window.Telegram?.WebApp;
                        if (!webApp) { return false; }
                        if (window.__tripsMainButtonHandler) {
                            webApp.offEvent('mainButtonClicked', window.__tripsMainButtonHandler);
                        }
                        window.__tripsMainButtonHandler = () => {
                            try {
                                const clientId = window.Telegram?.WebApp?.initDataUnsafe?.user?.id ?? null;
                                emitEvent('TRIPS_MAIN_BUTTON_CLICK', { clientId });
                            } catch (emitError) {
                                console.warn('emitEvent failed', emitError);
                            }
                        };
                        webApp.onEvent('mainButtonClicked', window.__tripsMainButtonHandler);
                        return true;
                    })();
                    """,
                    target=self._client,
                )
                await log_info(
                    "[trips][main_button] обработчик привязан",
                    type_msg="info",
                    event=event_name,
                )
            except Exception as js_error:  # noqa: BLE001
                await log_info(
                    "[trips][main_button] не удалось привязать обработчик",
                    type_msg="error",
                    reason=str(js_error),
                )

        await _emit_bound()
        self._bound = True

    async def set_state(
        self,
        *,
        text_key: str | None,
        visible: bool,
        enabled: bool,
        extra_kwargs: Optional[dict[str, Any]] = None,
    ) -> None:
        """Показываем/скрываем кнопку и меняем текст."""

        if text_key is None:
            target_text = ""
        else:
            target_text = lang_dict(text_key, self._lang, **(extra_kwargs or {}))

        js_code = json.dumps(target_text)
        show_flag = "true" if visible else "false"
        enable_flag = "true" if enabled else "false"

        try:
            await _safe_js(
                f"""
                (() => {{
                    const webApp = window.Telegram?.WebApp;
                    if (!webApp) {{ return false; }}
                    const button = webApp.MainButton;
                    if (!button) {{ return false; }}
                    if ({show_flag}) {{
                        button.setText({js_code});
                        button.show();
                    }} else {{
                        button.hide();
                    }}
                    if ({enable_flag}) {{
                        button.enable();
                    }} else {{
                        button.disable();
                    }}
                    return true;
                }})();
                """,
                target=self._client,
            )
            self._current_key = text_key
            self._is_visible = visible
            self._is_enabled = enabled
        except Exception as js_error:  # noqa: BLE001
            await log_info(
                "[trips][main_button] ошибка обновления состояния",
                type_msg="error",
                reason=str(js_error),
            )

    async def click(self) -> None:
        """Вызываем сохранённый обработчик, если он есть."""

        if self._action is None:
            await log_info(
                "[trips][main_button] обработчик не назначен",
                type_msg="warning",
            )
            return
        await self._action()

    def reset(self) -> None:
        """Отключаем кнопку в UI без JS (используется при очистке)."""

        self._current_key = None
        self._is_visible = False
        self._is_enabled = False
        self._action = None


class TripsTabView:
    """Главный контроллер вкладки «Поездки»"""

    def __init__(
        self,
        user_id: int | None,
        user_lang: str,
        user_data: dict[str, Any] | None,
        client: Any | None,
    ) -> None:
        self.user_id = user_id
        self.lang = user_lang or "en"
        self.user_data: dict[str, Any] = dict(user_data or {})
        self.client = client or getattr(ui.context, "client", None)
        self.role: str = "passenger"
        self.active_state = ActiveOrderState(role=self.role)
        self.main_button = MainButtonController(lang=self.lang, client=self.client)
        self.back_button = TelegramBackButton(client=self.client)
        self._poll_task: asyncio.Task[Any] | None = None
        self._lock = asyncio.Lock()
        self._timers: list[ui.timer] = []
        self._order_timer_label: ui.label | None = None
        self._awaiting_banner: ui.card | None = None
        self._panel_tabs: ui.tab_panels | None = None
        self._tabs_widget: ui.tabs | None = None
        self._offers_container: ui.column | None = None
        self._driver_stats_container: ui.column | None = None
        self._history_container: ui.column | None = None
        self._future_container: ui.column | None = None
        self._active_container: ui.column | None = None
        self._no_api_warning_shown: bool = False
        self.user_city: str = str(self.user_data.get("city") or "").strip()

    async def mount(self) -> None:
        """Создаём UI и запускаем загрузку данных."""

        await self._deactivate_back_button()
        app.storage.client["order_back_reset"] = self._deactivate_back_button
        await self.main_button.bind_handler(event_name="TRIPS_MAIN_BUTTON_CLICK", handler=self._on_main_button)
        ui.on(
            "TRIPS_MAIN_BUTTON_CLICK",
            lambda event: asyncio.create_task(self._handle_main_button_event(event.args if event else None)),
        )

        disconnect_cb = getattr(self.client, "on_disconnect", None)
        if callable(disconnect_cb):
            disconnect_cb(lambda: asyncio.create_task(self._cleanup()))

        with ui.column().classes("w-full q-pa-md gap-2 trips-tab"):
            ui.label(lang_dict("trips_title", self.lang)).classes("text-h6 text-left")
            self._build_role_loader()

        await self._load_initial_role()
        await self._render_role_specific_ui()
        await self._refresh_content()
        self._start_polling()

    def _build_role_loader(self) -> None:
        """Создаём контейнеры, которые будут обновляться по мере появления данных."""

        self._message_container = ui.column().classes("w-full gap-2")
        with self._message_container:
            self._message_label = ui.label(lang_dict("error_generic", self.lang)).classes(
                "text-body2 text-negative hidden"
            )

    async def _load_initial_role(self) -> None:
        """Определяем роль пользователя на основе user_data."""

        raw_role = self.user_data.get("role") if isinstance(self.user_data, dict) else None
        safe_role = str(raw_role or "passenger").lower()
        if safe_role not in {"driver", "passenger"}:
            safe_role = "passenger"
            await log_info(
                "[trips] роль в user_data отсутствует, используется passenger",
                type_msg="warning",
                user_id=self.user_id,
                role_raw=raw_role,
            )
        else:
            await log_info(
                "[trips] роль получена из user_data",
                type_msg="info",
                user_id=self.user_id,
                role=safe_role,
            )
        self.role = safe_role
        self.active_state.role = safe_role

    async def _render_role_specific_ui(self) -> None:
        """Создаём вкладки в зависимости от роли."""

        tabs_classes = "w-full shadow-none"
        tab_props = "dense no-caps align=justify narrow-indicator"

        self._tabs_widget = ui.tabs().props(tab_props).classes(tabs_classes)
        if self.role == "driver":
            with self._tabs_widget:
                ui.tab("available", label=lang_dict("driver_offers", self.lang), icon="list")
                ui.tab("active", label=lang_dict("driver_active", self.lang), icon="local_taxi")
                ui.tab("history", label=lang_dict("trips_history", self.lang), icon="history")
                ui.tab("stats", label=lang_dict("driver_stats", self.lang), icon="analytics")
        else:
            with self._tabs_widget:
                ui.tab("active", label=lang_dict("trips_active", self.lang), icon="directions_car")
                ui.tab("future", label=lang_dict("trips_future", self.lang), icon="schedule")
                ui.tab("history", label=lang_dict("trips_history", self.lang), icon="history")

        self._panel_tabs = (
            ui.tab_panels()
            .bind_value(self._tabs_widget, "value")
            .props("animated keep-alive transition-prev=fade transition-next=fade")
            .classes("w-full")
        )

        with self._panel_tabs:
            if self.role == "driver":
                with ui.tab_panel("available"):
                    self._offers_container = ui.column().classes("w-full gap-2")
                with ui.tab_panel("active"):
                    self._active_container = ui.column().classes("w-full gap-2")
                with ui.tab_panel("history"):
                    self._history_container = ui.column().classes("w-full gap-2")
                with ui.tab_panel("stats"):
                    self._driver_stats_container = ui.column().classes("w-full gap-2")
            else:
                with ui.tab_panel("active"):
                    self._active_container = ui.column().classes("w-full gap-2")
                with ui.tab_panel("future"):
                    self._future_container = ui.column().classes("w-full gap-2")
                with ui.tab_panel("history"):
                    self._history_container = ui.column().classes("w-full gap-2")

        if self.role == "driver":
            self._tabs_widget.set_value("available")
        else:
            self._tabs_widget.set_value("active")

    def _start_polling(self) -> None:
        """Запускаем периодический опрос REST API."""

        async def _poll_loop() -> None:
            # Цикл опроса API, чтобы UI оставался актуальным без ручного обновления.
            while True:
                try:
                    await self._refresh_content()
                except Exception as poll_error:  # noqa: BLE001
                    await log_info(
                        "[trips] ошибка в цикле опроса",
                        type_msg="error",
                        reason=str(poll_error),
                    )
                await asyncio.sleep(POLL_INTERVAL_SEC)

        if self._poll_task is not None:
            self._poll_task.cancel()
        self._poll_task = asyncio.create_task(_poll_loop())

    async def _refresh_content(self) -> None:
        """Обновляем состояние всех вкладок."""

        async with self._lock:
            if self.role == "driver":
                await asyncio.gather(
                    self._refresh_driver_offers(),
                    self._refresh_driver_active(),
                    self._refresh_history(role="driver"),
                    self._refresh_driver_stats(),
                )
            else:
                await asyncio.gather(
                    self._refresh_passenger_active(),
                    self._refresh_passenger_future(),
                    self._refresh_history(role="passenger"),
                )

    async def _refresh_passenger_active(self) -> None:
        """Загружаем активный заказ пассажира."""

        try:
            if not self.user_id:
                self.active_state.order = None
                self.active_state.need_topup = False
                self.active_state.awaiting_fee_deadline = None
                await self._render_passenger_active()
                await self._maybe_switch_main_button_passenger()
                return

            order_id = await get_latest_open_order_id_for_passenger(int(self.user_id))
            raw_order = await get_order_data(int(order_id)) if order_id else None
            normalized_order = self._normalize_order(raw_order)
            self.active_state.order = normalized_order
            self.active_state.need_topup = bool((normalized_order or {}).get("need_topup"))
            self.active_state.awaiting_fee_deadline = None
            await self._render_passenger_active()
            await self._maybe_switch_main_button_passenger()
        except Exception as error:  # noqa: BLE001
            await self._handle_api_error(error)

    async def _refresh_passenger_future(self) -> None:
        """Загружаем будущие поездки пассажира."""

        if self._future_container is None:
            return
        try:
            if not self.user_id:
                await self._render_future_orders([])
                return
            items = await list_future_orders_for_passenger(int(self.user_id), limit=20)
            await self._render_future_orders(self._normalize_orders(items))
        except Exception as error:  # noqa: BLE001
            await self._handle_api_error(error)

    async def _refresh_driver_offers(self) -> None:
        """Загружаем офферы для водителя."""

        if self._offers_container is None:
            return
        try:
            driver_id = int(self.user_id) if self.user_id else None
            if driver_id is None:
                await self._render_driver_offers([])
                return
            offers = await list_available_orders_for_driver(
                city=self.user_city or None,
                limit=20,
                exclude_user_id=driver_id,
            )
            await self._render_driver_offers(self._normalize_orders(offers))
        except Exception as error:  # noqa: BLE001
            await self._handle_api_error(error)

    async def _refresh_driver_active(self) -> None:
        """Загружаем активный заказ для водителя."""

        try:
            driver_id = int(self.user_id) if self.user_id else None
            if driver_id is None:
                self.active_state.order = None
                self.active_state.need_topup = False
                self.active_state.awaiting_fee_deadline = None
                await self._render_driver_active()
                await self._maybe_switch_main_button_driver()
                return
            raw_order = await get_active_order_for_driver(driver_id)
            normalized_order = self._normalize_order(raw_order)
            self.active_state.order = normalized_order
            self.active_state.need_topup = bool((normalized_order or {}).get("need_topup"))
            self.active_state.awaiting_fee_deadline = None
            await self._render_driver_active()
            await self._maybe_switch_main_button_driver()
        except Exception as error:  # noqa: BLE001
            await self._handle_api_error(error)

    async def _refresh_driver_stats(self) -> None:
        """Обновляем статистику водителя."""

        if self._driver_stats_container is None:
            return
        try:
            driver_id = int(self.user_id) if self.user_id else None
            if driver_id is None:
                await self._render_driver_stats({})
                return
            stats = await get_driver_stats_summary(driver_id)
            await self._render_driver_stats(stats or {})
        except Exception as error:  # noqa: BLE001
            await self._handle_api_error(error)

    async def _refresh_history(self, *, role: str) -> None:
        """Отрисовка истории поездок."""

        if self._history_container is None:
            return
        try:
            if not self.user_id:
                await self._render_history([])
                return
            history = await list_order_history_for_user(int(self.user_id), role=role, limit=20)
            await self._render_history(self._normalize_orders(history))
        except Exception as error:  # noqa: BLE001
            await self._handle_api_error(error)

    async def _render_passenger_active(self) -> None:
        if self._active_container is None:
            return
        self._active_container.clear()

        order = self.active_state.order
        if not order:
            with self._active_container:
                ui.label(lang_dict("trips_future", self.lang)).classes("text-body1 text-secondary")
            return

        await self._render_route_card(order, container=self._active_container)
        await self._render_action_buttons(order, container=self._active_container)
        await self._render_awaiting_banner(order, container=self._active_container)

    async def _render_future_orders(self, items: list[dict[str, Any]]) -> None:
        if self._future_container is None:
            return
        self._future_container.clear()
        if not items:
            with self._future_container:
                ui.label(lang_dict("trips_history", self.lang)).classes("text-body1 text-secondary")
            return
        for item in items:
            with self._future_container:
                with ui.card().classes("w-full bg-grey-1 text-dark"):
                    ui.label(self._format_route_title(item)).classes("text-body1 text-weight-medium")
                    ui.label(self._format_schedule_line(item)).classes("text-caption text-secondary")
                    order_id = item.get("order_id")
                    ui.button(
                        lang_dict("cancel", self.lang),
                        on_click=lambda _e, oid=order_id: asyncio.create_task(self._cancel_order(oid)),
                    ).props("outline color=negative size=small")

    async def _render_driver_offers(self, offers: list[dict[str, Any]]) -> None:
        if self._offers_container is None:
            return
        self._offers_container.clear()
        if not offers:
            with self._offers_container:
                ui.label(lang_dict("driver_offers", self.lang)).classes("text-body1 text-secondary")
            await self.main_button.set_state(text_key=None, visible=False, enabled=False)
            return

        for offer in offers:
            with self._offers_container:
                with ui.card().classes("w-full bg-grey-1 text-dark"):
                    ui.label(self._format_route_title(offer)).classes("text-body1 text-weight-medium")
                    ui.label(self._format_offer_meta(offer)).classes("text-caption text-secondary")
                    commission = int(offer.get("commission_stars") or 0)
                    ui.label(
                        lang_dict("need_topup_stars", self.lang, amount=commission)
                    ).classes("text-caption text-warning")
                    order_id = offer.get("order_id")
                    ui.button(
                        lang_dict("accept_offer", self.lang),
                        on_click=lambda _e, oid=order_id: asyncio.create_task(self._accept_offer(oid)),
                    ).props("color=primary size=small")
        await self.main_button.set_state(
            text_key="accept_offer",
            visible=True,
            enabled=True,
        )

    async def _render_driver_active(self) -> None:
        if self._active_container is None:
            return
        self._active_container.clear()
        order = self.active_state.order
        if not order:
            with self._active_container:
                ui.label(lang_dict("driver_active", self.lang)).classes("text-body1 text-secondary")
            await self.main_button.set_state(text_key=None, visible=False, enabled=False)
            return

        await self._render_route_card(order, container=self._active_container)
        await self._render_driver_stage_controls(order, container=self._active_container)
        await self._render_awaiting_banner(order, container=self._active_container)

    async def _render_driver_stats(self, stats: dict[str, Any]) -> None:
        if self._driver_stats_container is None:
            return
        self._driver_stats_container.clear()
        if not stats:
            with self._driver_stats_container:
                ui.label(lang_dict("driver_stats", self.lang)).classes("text-body1 text-secondary")
            return

        with self._driver_stats_container:
            ui.label(lang_dict("driver_stats", self.lang)).classes("text-subtitle1 text-weight-medium")
            labels = {
                "completed": lang_dict("completed", self.lang),
                "active": lang_dict("driver_active", self.lang),
                "canceled": lang_dict("cancel", self.lang),
            }
            for key in ("completed", "active", "canceled", "total_commission", "total_revenue"):
                if key not in stats:
                    continue
                label = labels.get(key, key.replace("_", " "))
                ui.label(f"{label}: {stats[key]}").classes("text-body2")

    async def _render_history(self, history: list[dict[str, Any]]) -> None:
        if self._history_container is None:
            return
        self._history_container.clear()
        if not history:
            with self._history_container:
                ui.label(lang_dict("trips_history", self.lang)).classes("text-body1 text-secondary")
            return
        for item in history:
            with self._history_container:
                with ui.card().classes("w-full bg-grey-1 text-dark"):
                    ui.label(self._format_route_title(item)).classes("text-body1 text-weight-medium")
                    ui.label(self._format_history_meta(item)).classes("text-caption text-secondary")
                    order_id = item.get("order_id")
                    ui.button(
                        lang_dict("accept_offer", self.lang),
                        on_click=lambda _e, oid=order_id: asyncio.create_task(self._repeat_order(oid)),
                    ).props("size=small color=primary outline")

    async def _render_route_card(self, order: dict[str, Any], *, container: ui.column) -> None:
        with container:
            with ui.card().classes("w-full bg-grey-1 text-dark"):
                ui.label(self._format_route_title(order)).classes("text-body1 text-weight-medium")
                ui.label(self._format_schedule_line(order)).classes("text-caption text-secondary")
                ui.label(self._format_status_line(order)).classes("text-caption")
                if order.get("eta_pickup") or order.get("eta_dropoff"):
                    eta_line = self._format_eta_line(order)
                    ui.label(eta_line).classes("text-caption text-secondary")

    async def _render_action_buttons(self, order: dict[str, Any], *, container: ui.column) -> None:
        with container:
            with ui.row().classes("gap-2"):
                ui.button(
                    lang_dict("call", self.lang),
                    on_click=lambda _e: asyncio.create_task(self._open_contact(order, mode="call")),
                ).props("outline size=small")
                ui.button(
                    lang_dict("message", self.lang),
                    on_click=lambda _e: asyncio.create_task(self._open_contact(order, mode="message")),
                ).props("outline size=small")
                ui.button(
                    lang_dict("cancel", self.lang),
                    on_click=lambda _e: asyncio.create_task(self._confirm_cancel(order.get("order_id"))),
                ).props("color=negative outline size=small")

    async def _render_driver_stage_controls(self, order: dict[str, Any], *, container: ui.column) -> None:
        with container:
            stages = ["in_place", "come_out", "started", "completed"]
            current = str(order.get("status") or "")

            buttons_row = ui.row().classes("gap-2 wrap")
            for stage in stages:
                order_id = order.get("order_id")

                def _make_handler(target_stage: str, oid: Any) -> Callable[[Any], None]:
                    return lambda _e, st=target_stage, target_order_id=oid: asyncio.create_task(
                        self._transition_order(target_order_id, st)
                    )

                button = ui.button(
                    lang_dict(stage, self.lang),
                    on_click=_make_handler(stage, order_id),
                )
                button_props = "size=small"
                if stage == current:
                    button_props += " color=primary"
                else:
                    button_props += " outline"
                button.props(button_props)
                buttons_row.add(button)
            order_id = order.get("order_id")
            ui.button(
                lang_dict("cancel", self.lang),
                on_click=lambda _e, oid=order_id: asyncio.create_task(self._confirm_cancel(oid)),
            ).props("color=negative outline size=small")

    async def _render_awaiting_banner(self, order: dict[str, Any], *, container: ui.column) -> None:
        needs_topup = bool(order.get("need_topup"))
        status = str(order.get("status") or "")
        if not needs_topup and status != "awaiting_fee":
            if self._awaiting_banner is not None:
                self._awaiting_banner.clear()
            self._cancel_timers()
            return

        self._cancel_timers()
        description = lang_dict("awaiting_fee", self.lang)
        with container:
            self._awaiting_banner = ui.card().classes("w-full bg-warning text-dark")
            with self._awaiting_banner:
                ui.label(description).classes("text-body2 text-weight-medium")
                self._order_timer_label = ui.label(self._deadline_text()).classes("text-caption")
                ui.button(
                    lang_dict("need_topup_stars", self.lang, amount=int(order.get("commission_stars") or 0)),
                    on_click=lambda _e: asyncio.create_task(self._open_topup()),
                ).props("color=primary size=small")
        self._install_deadline_timer()

    def _install_deadline_timer(self) -> None:
        if self._order_timer_label is None:
            return
        timer = ui.timer(1.0, self._update_deadline_label)
        self._timers.append(timer)

    def _update_deadline_label(self) -> None:
        if self._order_timer_label is None:
            return
        self._order_timer_label.set_text(self._deadline_text())

    def _cancel_timers(self) -> None:
        """Останавливаем таймеры, чтобы не плодить параллельные задачи."""

        for timer in self._timers:
            try:
                timer.active = False
            except Exception:
                pass
        self._timers.clear()
        self._order_timer_label = None

    def _deadline_text(self) -> str:
        deadline = self.active_state.awaiting_fee_deadline
        if not deadline:
            return ""
        remaining = deadline - datetime.utcnow()
        if remaining.total_seconds() <= 0:
            return lang_dict("awaiting_fee", self.lang)
        minutes, seconds = divmod(int(remaining.total_seconds()), 60)
        return f"{minutes:02d}:{seconds:02d}"

    async def _maybe_switch_main_button_passenger(self) -> None:
        order = self.active_state.order
        if not order:
            await self.main_button.set_state(text_key="accept_offer", visible=True, enabled=True)
            return
        status = str(order.get("status") or "")
        if status == "awaiting_fee":
            await self.main_button.set_state(
                text_key="need_topup_stars",
                visible=True,
                enabled=True,
                extra_kwargs={"amount": int(order.get("commission_stars") or 0)},
            )
        elif status == "accepted":
            await self.main_button.set_state(text_key="order_accepted", visible=True, enabled=False)
        else:
            await self.main_button.set_state(text_key="trips_active", visible=False, enabled=False)

    async def _maybe_switch_main_button_driver(self) -> None:
        order = self.active_state.order
        if not order:
            await self.main_button.set_state(text_key="accept_offer", visible=True, enabled=True)
            return
        status = str(order.get("status") or "")
        if status in {"in_place", "come_out", "started"}:
            await self.main_button.set_state(text_key="completed", visible=True, enabled=True)
        elif status == "completed":
            await self.main_button.set_state(text_key="completed", visible=True, enabled=False)
        elif status == "awaiting_fee":
            await self.main_button.set_state(
                text_key="need_topup_stars",
                visible=True,
                enabled=True,
                extra_kwargs={"amount": int(order.get("commission_stars") or 0)},
            )
        else:
            await self.main_button.set_state(text_key="driver_active", visible=False, enabled=False)
    def _normalize_order(self, payload: dict[str, Any] | None) -> dict[str, Any] | None:
        """Приводим словарь заказа к унифицированному виду для UI."""

        if not isinstance(payload, dict):
            return None

        order = dict(payload)
        # Адреса: поддерживаем как REST-ответы, так и структуру из orders
        order.setdefault("pickup_address", order.get("address_from"))
        order.setdefault("dropoff_address", order.get("address_to"))

        # Даты преобразуем к ISO-строкам, чтобы NiceGUI не падал на datetime
        for key in ("order_date", "scheduled_at", "created_at", "trip_start", "trip_end", "in_place_at"):
            value = order.get(key)
            if isinstance(value, datetime):
                order[key] = value.isoformat()

        if not order.get("created_at") and order.get("order_date"):
            order["created_at"] = order.get("order_date")

        order["status"] = str(order.get("status") or "")
        order["commission_stars"] = int(order.get("commission_stars") or 0)
        order["need_topup"] = bool(order.get("need_topup"))
        return order

    def _normalize_orders(self, items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """Применяем нормализацию к списку заказов."""

        if not items:
            return []
        normalized: list[dict[str, Any]] = []
        for item in items:
            normalized_item = self._normalize_order(item)
            if normalized_item:
                normalized.append(normalized_item)
        return normalized

    async def _handle_api_error(self, error: Exception) -> None:
        await log_info(
            "[trips] ошибка получения данных",
            type_msg="error",
            reason=str(error),
        )
        if not self._no_api_warning_shown:
            ui.notify(lang_dict("error_generic", self.lang), type="warning")
            self._no_api_warning_shown = True

    async def _cancel_order(self, order_id: Any) -> None:
        if not order_id or not self.user_id:
            return
        try:
            initiator = int(self.user_id)
            try:
                order_numeric = int(order_id)
            except (TypeError, ValueError) as cast_error:
                raise RuntimeError(f"invalid order_id: {order_id!r}") from cast_error
            result = await cancel_order(order_numeric, initiator)
            if result is None:
                raise RuntimeError("cancel_order returned None")
            await log_info(
                "[trips] заказ отменён",
                type_msg="info",
                order_id=order_id,
            )
            await self._refresh_content()
        except Exception as error:  # noqa: BLE001
            await self._handle_api_error(error)

    async def _confirm_cancel(self, order_id: Any) -> None:
        if not order_id or not self.user_id:
            return

        confirmation = ui.dialog().props("persistent")

        async def _close_dialog(_: Any | None = None) -> None:
            confirmation.close()
            await self._deactivate_back_button()

        async def _handle_back() -> None:
            await _close_dialog()

        await self._activate_back_button(_handle_back)
        try:
            with confirmation:
                with ui.card().classes("w-full"):
                    ui.label(lang_dict("confirm_cancel_title", self.lang)).classes("text-subtitle1 text-weight-medium")
                    ui.label(lang_dict("confirm_cancel_desc", self.lang)).classes("text-body2")
                    with ui.row().classes("justify-end gap-2"):
                        ui.button(
                            lang_dict("cancel", self.lang),
                            on_click=_close_dialog,
                        ).props("flat")
                        ui.button(
                            lang_dict("confirm", self.lang),
                            on_click=lambda _e: asyncio.create_task(
                                self._cancel_and_close(confirmation, order_id)
                            ),
                        ).props("color=negative")
        except Exception:
            await self._deactivate_back_button()
            raise

        confirmation.open()

    async def _cancel_and_close(self, dialog: ui.dialog, order_id: Any) -> None:
        dialog.close()
        await self._deactivate_back_button()
        await self._cancel_order(order_id)

    async def _accept_offer(self, order_id: Any) -> None:
        if not order_id or not self.user_id:
            return
        try:
            driver_id = int(self.user_id)
            try:
                order_numeric = int(order_id)
            except (TypeError, ValueError) as cast_error:
                raise RuntimeError(f"invalid order_id: {order_id!r}") from cast_error
            result = await reserve_order(order_numeric, driver_id)
            if result is None:
                raise RuntimeError("reserve_order returned None")
            await self._refresh_content()
        except Exception as error:  # noqa: BLE001
            await self._handle_api_error(error)

    async def _transition_order(self, order_id: Any, stage: str) -> None:
        if not order_id or not self.user_id:
            return
        try:
            driver_id = int(self.user_id)
            try:
                order_numeric = int(order_id)
            except (TypeError, ValueError) as cast_error:
                raise RuntimeError(f"invalid order_id: {order_id!r}") from cast_error
            success = False
            if stage == "in_place":
                success = await mark_driver_arrived(order_numeric)
            elif stage == "come_out":
                success = await mark_passenger_comeout(order_numeric)
            elif stage == "started":
                success = await mark_trip_started(order_numeric, driver_id)
            elif stage == "completed":
                passenger_id = await complete_order(order_numeric, driver_id)
                success = passenger_id is not None
            else:
                await log_info(
                    "[trips] неизвестный этап перехода заказа",
                    type_msg="warning",
                    order_id=order_id,
                    stage=stage,
                )
                return

            if not success:
                raise RuntimeError(f"transition {stage} failed")
            await self._refresh_content()
        except Exception as error:  # noqa: BLE001
            await self._handle_api_error(error)

    async def _repeat_order(self, order_id: Any) -> None:
        if not order_id:
            return
        try:
            await log_info(
                "[trips] повтор заказа недоступен",
                type_msg="warning",
                order_id=order_id,
            )
            ui.notify(lang_dict("error_generic", self.lang), type="warning")
        except Exception as error:  # noqa: BLE001
            await self._handle_api_error(error)

    async def _open_contact(self, order: dict[str, Any], *, mode: str) -> None:
        target = order.get("support_chat") if mode == "message" else order.get("support_phone")
        if not target:
            await log_info(
                "[trips] контакт недоступен",
                type_msg="warning",
                mode=mode,
            )
            return
        if mode == "message":
            js_code = f"window.Telegram?.WebApp?.openTelegramLink('https://t.me/{target}')"
        else:
            js_code = f"window.location.href='tel:{target}'"
        try:
            await _safe_js(f"(() => {{ {js_code}; return true; }})();", target=self.client)
        except Exception as error:  # noqa: BLE001
            await self._handle_api_error(error)

    async def _open_topup(self) -> None:
        try:
            await _safe_js(
                "(() => { window.Telegram?.WebApp?.openTelegramLink(window.Telegram?.WebApp?.initDataUnsafe?.start_param || 'https://t.me/'); return true; })();",
                target=self.client,
            )
        except Exception as error:  # noqa: BLE001
            await self._handle_api_error(error)

    async def _subscribe_to_order_ws(self) -> None:
        """В текущей реализации вебсокеты не используются."""
        return

    async def _cleanup(self) -> None:
        """Очищаем фоновые задачи при отключении клиента."""

        self._cancel_timers()
        if self._poll_task:
            self._poll_task.cancel()
        await self._deactivate_back_button()
        app.storage.client.pop("order_back_reset", None)
        await log_info(
            "[trips] клиент отключился, фоновые задачи остановлены",
            type_msg="info",
            user_id=self.user_id,
        )

    async def _handle_main_button_event(self, payload: Any) -> None:
        """Фильтруем события главной кнопки по текущему пользователю."""

        client_id: Any | None = None
        if isinstance(payload, dict):
            client_id = payload.get("clientId")
        if client_id and self.user_id and int(client_id) != int(self.user_id):
            return
        await self.main_button.click()

    async def _activate_back_button(self, handler: Callable[[], Awaitable[None]]) -> None:
        """Показываем штатную кнопку Telegram Back и навешиваем обработчик."""

        try:
            await self.back_button.activate(handler)
        except Exception as error:  # noqa: BLE001
            await log_info(
                "[trips][back_button] не удалось активировать",
                type_msg="error",
                reason=str(error),
                user_id=self.user_id,
            )

    async def _deactivate_back_button(self) -> None:
        """Скрываем кнопку Telegram Back, если она была показана."""

        try:
            await self.back_button.deactivate()
        except Exception as error:  # noqa: BLE001
            await log_info(
                "[trips][back_button] не удалось скрыть",
                type_msg="error",
                reason=str(error),
                user_id=self.user_id,
            )

    def _format_route_title(self, order: dict[str, Any]) -> str:
        pickup = order.get("pickup_address") or order.get("address_from") or "—"
        dropoff = order.get("dropoff_address") or order.get("address_to") or "—"
        return f"{pickup} → {dropoff}"

    def _format_schedule_line(self, order: dict[str, Any]) -> str:
        scheduled = order.get("scheduled_at")
        created = order.get("created_at")
        for value in (scheduled, created):
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, str) and value:
                return value
        return ""

    def _format_status_line(self, order: dict[str, Any]) -> str:
        status = str(order.get("status") or "")
        try:
            return lang_dict(status, self.lang)
        except KeyError:
            return status

    def _format_eta_line(self, order: dict[str, Any]) -> str:
        pickup_eta = order.get("eta_pickup")
        dropoff_eta = order.get("eta_dropoff")
        parts: list[str] = []
        if pickup_eta:
            parts.append(f"ETA pickup: {pickup_eta}")
        if dropoff_eta:
            parts.append(f"ETA dropoff: {dropoff_eta}")
        return " | ".join(parts)

    def _format_offer_meta(self, offer: dict[str, Any]) -> str:
        distance_m = offer.get("distance_m")
        distance_km = offer.get("distance_km")
        if distance_m is not None:
            distance_text = f"{distance_m} m"
        elif distance_km is not None:
            distance_text = f"{distance_km} km"
        else:
            distance_text = "—"

        eta = offer.get("scheduled_at") or offer.get("order_date")
        if isinstance(eta, datetime):
            eta_text = eta.isoformat()
        else:
            eta_text = str(eta) if eta else "—"
        return f"{distance_text} · {eta_text}"

    def _format_history_meta(self, order: dict[str, Any]) -> str:
        status = self._format_status_line(order)
        completed = order.get("trip_end") or order.get("completed_at") or order.get("order_date")
        if isinstance(completed, datetime):
            completed_text = completed.isoformat()
        else:
            completed_text = str(completed) if completed else "—"
        return f"{status} · {completed_text}"

    async def _repeat_order_from_main_button(self) -> None:
        order = self.active_state.order
        await self._repeat_order(order.get("order_id") if order else None)

    async def _on_main_button(self) -> None:
        if self.role == "driver":
            order = self.active_state.order
            if not order:
                await self._refresh_driver_offers()
                return
            status = str(order.get("status") or "")
            if status in {"in_place", "come_out", "started"}:
                await self._transition_order(order.get("order_id"), "completed")
            elif status == "awaiting_fee":
                await self._open_topup()
            else:
                await self._refresh_driver_active()
        else:
            order = self.active_state.order
            if not order:
                await log_info(
                    "[trips] запрос на создание заказа из веба не поддерживается",
                    type_msg="warning",
                    user_id=self.user_id,
                )
                ui.notify(lang_dict("error_generic", self.lang), type="warning")
            elif str(order.get("status") or "") == "awaiting_fee":
                await self._open_topup()
            else:
                await self._refresh_passenger_active()


async def render_order_tab(
    user_id: int | None,
    user_lang: str,
    user_data: dict[str, Any] | None,
) -> None:
    """Рендер вкладки «Поездки» в основном UI."""

    client = getattr(ui.context, "client", None)
    view = TripsTabView(user_id=user_id, user_lang=user_lang, user_data=user_data, client=client)
    try:
        await view.mount()
    except Exception as error:  # noqa: BLE001
        await log_info(
            "[trips] критическая ошибка рендера",
            type_msg="error",
            reason=str(error),
            user_id=user_id,
        )
        ui.notify(lang_dict("error_generic", user_lang or "en"), type="negative")
