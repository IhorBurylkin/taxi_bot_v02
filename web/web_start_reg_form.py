from __future__ import annotations

from nicegui import ui
from contextlib import nullcontext
import asyncio
from db.db_utils import user_exists, update_table, insert_into_table
from config.config_from_db import load_cities, load_country_choices
from web.web_utilits import _save_upload, bind_enter_action, TelegramBackButton
from config.config_utils import lang_dict
from keyboards.inline_kb_verification import verification_inline_kb
from log.log import log_info, send_info_msg


finish_lock = asyncio.Lock()


async def start_reg_form_ui(uid, user_lang, user_data, choice_role) -> None:
    await ui.run_javascript('''
        try{
        const tg = window.Telegram?.WebApp;
        tg?.ready?.();
        tg?.expand?.();                     // полноэкранная высота
        tg?.disableVerticalSwipes?.();       // не блокируем вертикальные свайпы
        }catch(e){}
        ''', timeout=3.0)
    def _enter_dbg(e):
        p = e.args[0] if isinstance(e.args, list) else e.args
        print('ENTER-DBG:', p)
    ui.on('enter_dbg', _enter_dbg)
    cities = await load_cities()
    COUNTRY_CHOICES = await load_country_choices()
    model = {
        'role': 'driver' if not choice_role else None,
        'city': None,
        'phone_passenger': None, 'phone_driver': None,
        'car_brand': None, 'car_model': None, 'car_color': None, 'car_number': None,
        'car_image': None, 'techpass_image': None, 'driver_license': None,
    }

    back_button = TelegramBackButton()
    await back_button.deactivate()
    step_sequence: list[str] = []
    step_indexes: dict[str, int] = {}
    current_step_index = {"value": 0}

    async def _on_submit_driver_form(e):
        # если нужно — здесь обновляете вашу модель/БД
        ui.notify(lang_dict('notify_ok_data', user_lang), type='positive')

    with ui.element('div').classes('w-full min-h-[calc(var(--vh,1vh)*100)] mx-auto px-4 md:px-4') as page_root:
        #with ui.scroll_area().classes('vscroll'):
        container_ctx = (
            ui.column().classes('w-full page-center q-gutter-y-md')
            if choice_role else nullcontext()
        )
        with container_ctx:
            if choice_role:
                ui.label(lang_dict('title_registration', user_lang)).classes('text-2xl font-bold gap-2 q-ma-none q-mt-lg q-pa-none text-center self-center')
            with ui.stepper().props('vertical').classes('w-full q-pa-none q-ma-none') as st:
                async def _step_next(context: str | None = None) -> None:
                    """Переключает степпер на следующий шаг с логированием ошибок."""
                    try:
                        st.next()
                    except Exception as nav_error:  # noqa: BLE001
                        await log_info(
                            "[start_reg_form] ошибка перехода вперёд",
                            type_msg="error",
                            uid=uid,
                            context=context,
                            reason=str(nav_error),
                        )

                async def _handle_back_click() -> None:
                    """Возвращает пользователя на предыдущий шаг через Telegram BackButton."""
                    idx = current_step_index["value"]
                    if idx <= 0 or not step_sequence:
                        return
                    prev_name = step_sequence[idx - 1]
                    try:
                        st.set_value(prev_name)
                    except Exception as nav_error:  # noqa: BLE001
                        try:
                            st.previous()
                        except Exception as fallback_error:  # noqa: BLE001
                            await log_info(
                                "[start_reg_form] ошибка перехода назад",
                                type_msg="error",
                                uid=uid,
                                reason=f"{nav_error}; fallback={fallback_error}",
                            )

                async def _sync_back_button(step_name: str | None) -> None:
                    """Обновляет состояние Telegram BackButton в зависимости от текущего шага."""
                    try:
                        if not step_sequence:
                            current_step_index["value"] = 0
                            await back_button.deactivate()
                            return
                        base_step = step_sequence[0]
                        idx = step_indexes.get(step_name or base_step, 0)
                        current_step_index["value"] = idx
                        if idx > 0:
                            await back_button.activate(_handle_back_click)
                        else:
                            await back_button.deactivate()
                    except Exception as sync_error:  # noqa: BLE001
                        await log_info(
                            "[start_reg_form] ошибка синхронизации BackButton",
                            type_msg="error",
                            uid=uid,
                            reason=str(sync_error),
                        )

                # 1) Роль
                if choice_role:
                    with ui.step(lang_dict('step_role', user_lang)).props('name=role').classes('w-full q-pa-none q-ma-none'):
                        role = ui.radio({'passenger': lang_dict('role_passenger', user_lang), 'driver': lang_dict('role_driver', user_lang)}, value=None).props('inline color=primary keep-color')
                        next1 = ui.button(lang_dict('btn_next', user_lang)); next1.disable()

                        def on_role_change(e):
                            model['role'] = e.value
                            next1.enable() if e.value else next1.disable()   # только включаем кнопку
                        role.on_value_change(on_role_change)

                # 2) Город (из config.cities)
                with ui.step(lang_dict('step_city', user_lang)).props('name=city').classes('w-full q-pa-none q-ma-none') as step_city:
                    tree = cities or {}
                    if not isinstance(tree, dict):
                        tree = {'—': {'—': [str(x) for x in (tree or [])]}}
                    countries = sorted(tree.keys())

                    with ui.column().classes('w-full gap-2'):
                        sel_country = ui.select(countries, label=lang_dict('label_country', user_lang), with_input=False, value=None).classes('w-full')
                        sel_region  = ui.select([],        label=lang_dict('label_region', user_lang),  with_input=False, value=None).classes('w-full')
                        sel_city    = ui.select([],        label=lang_dict('label_locality', user_lang), with_input=False, value=None).classes('w-full')

                        # ИЗНАЧАЛЬНО БЛОКИРУЕМ ЗАВИСИМЫЕ СЕЛЕКТЫ
                        sel_region.disable()
                        sel_city.disable()

                    with ui.stepper_navigation().classes('w-full q-pa-none q-ma-none'):
                        async def _on_city_next(_):
                            await _step_next('city')

                        next2 = ui.button(lang_dict('btn_next', user_lang), on_click=_on_city_next); next2.disable()

                    def _rebuild_regions(country: str | None):
                        # Всегда сбрасываем и блокируем зависимые
                        sel_region.options = []; sel_region.value = None; sel_region.disable(); sel_region.update()
                        sel_city.options   = []; sel_city.value   = None; sel_city.disable();   sel_city.update()

                        if not country:
                            _toggle_next()
                            return

                        node = tree.get(country)
                        if isinstance(node, dict):
                            regions = sorted(node.keys())
                            sel_region.options = regions
                            sel_region.value = None
                            sel_region.enable()               # страна выбрана → разрешаем выбор региона
                            sel_region.update()
                            # города появятся после выбора региона
                        elif isinstance(node, list):
                            # страна без регионов
                            sel_region.options = ['—']
                            sel_region.value = '—'
                            sel_region.disable()              # регион фиксированный, не редактируем
                            sel_region.update()
                            _rebuild_cities(country, '—')     # сразу подставим города для страны без регионов

                        _toggle_next()

                    def _rebuild_cities(country: str | None, region: str | None):
                        cities_list: list[str] = []
                        if country and region:
                            node = tree.get(country)
                            if isinstance(node, dict):
                                cities_list = sorted(node.get(region, []) or [])
                            elif isinstance(node, list) and region == '—':
                                cities_list = sorted(node)
                        sel_city.options = cities_list
                        sel_city.value = None
                        if cities_list:
                            sel_city.enable()
                        else:
                            sel_city.disable()
                        sel_city.update()
                        _toggle_next()

                    def _toggle_next():
                        ok = bool(sel_country.value and (sel_region.value or sel_region.options == ['—']) and sel_city.value)
                        next2.enable() if ok else next2.disable()
                        if ok:
                            model['country'] = sel_country.value
                            model['region']  = sel_region.value
                            model['city']    = sel_city.value

                    sel_country.on_value_change(lambda e: (_rebuild_regions(e.value)))
                    sel_region.on_value_change(lambda e: (_rebuild_cities(sel_country.value, e.value)))
                    sel_city.on_value_change(lambda e: (_toggle_next()))

                step_city.visible = False

                # 3) Телефон (для водителя — дальше; для пассажира — завершить)
                with ui.step(lang_dict('step_phone', user_lang)).props('name=phone').classes('w-full q-pa-none q-ma-none') as step_phone:
                    async def _submit_phone(_):
                        if model.get('role') == 'driver':
                            await _step_next('phone')
                        else:
                            await _finish_passenger(None)

                    META = {c: {'flag': flag, 'name': name, 'area': int(a), 'rest': int(b)}
                            for (c, flag, name, a, b) in COUNTRY_CHOICES}

                    # options как dict {value: label}; label кодируем "flag|name|code" для слотов
                    OPTIONS = {c: f'{META[c]["flag"]}|{META[c]["name"]}|{c}' for c, *_ in COUNTRY_CHOICES}
                    DEFAULT_CODE = COUNTRY_CHOICES[0][0]


                    with ui.element('form').on('submit.prevent', _submit_phone):
                        ui.element('input').props('type=submit hidden')

                        with ui.row().classes('items-stretch w-full max-w-full gap-2 no-wrap overflow-x-hidden'):
                            code = (ui.select(OPTIONS, label=lang_dict('label_code', user_lang), value=DEFAULT_CODE)
                                    .classes('w-22 shrink-0 self-stretch'))

                            # selected: флаг + код
                            code.add_slot('selected-item', r"""
                            <div class="row items-center no-wrap q-gutter-x-xs">
                            <span>{{ props.opt.label.split('|')[0] }}</span>
                            <span>{{ props.opt.label.split('|')[2] }}</span>
                            </div>
                            """)

                            # option: флаг, страна, код
                            code.add_slot('option', r"""
                            <q-item v-bind="props.itemProps">
                            <q-item-section avatar>
                                <div>{{ props.opt.label.split('|')[0] }}</div>
                            </q-item-section>
                            <q-item-section>
                                <q-item-label>{{ props.opt.label.split('|')[1] }}</q-item-label>
                                <q-item-label caption>{{ props.opt.label.split('|')[2] }}</q-item-label>
                            </q-item-section>
                            </q-item>
                            """)

                            # динамическая длина; используем dict, чтобы менять замыкание "на лету"
                            required = {'n': META[DEFAULT_CODE]['area'] + META[DEFAULT_CODE]['rest']}

                            number = (ui.input(lang_dict('label_phone', user_lang), 
                                            validation={lang_dict('validation_wrong_length', user_lang): lambda v: (v or '').isdigit() and len(v) == required['n']})
                                    .props('type=tel inputmode=tel unmasked-value enterkeyhint=done')
                                    .classes('flex-1 min-w-0 self-stretch'))

                        with ui.stepper_navigation().classes('w-full q-pa-none q-ma-none'):
                            btn_next3   = ui.button(lang_dict('btn_next', user_lang)).props('type=submit')
                            btn_finish3 = ui.button(lang_dict('btn_finish', user_lang)).props('type=submit')

                    # ——— логика маски и синхронизация ———
                    def _apply_mask_for(code_val: str):
                        a = META[code_val]['area']   # цифр в скобках
                        b = META[code_val]['rest']   # цифр после
                        required['n'] = a + b
                        # Группы для rest: 3-2-2 (если b=7). Для других b — аккуратно «доделываем» остаток.
                        if b <= 3:
                            groups = [b]
                        elif b <= 5:
                            groups = [3, b - 3]
                        elif b <= 7:
                            groups = [3, 2, b - 5]
                        else:
                            groups = [3, 2, 2, b - 7]  # на случай rest > 7

                        mask_tail = '-'.join('#' * g for g in groups if g > 0)
                        mask = f"({('#' * a)}) {mask_tail}"
                        number.props(f'mask="{mask}" unmasked-value')
                        number.update()
                        number.validate()

                    def _sync_phone(*_):
                        d = (number.value or '')                  # благодаря unmasked-value здесь только цифры
                        ok = (len(d) == required['n'])
                        full = f"{code.value}{d}" if ok else None
                        if model.get('role') == 'driver':
                            model['phone_driver'] = full
                            btn_next3.visible, btn_finish3.visible = True, False
                            (btn_next3.enable() if ok else btn_next3.disable())
                        else:
                            model['phone_passenger'] = full
                            btn_next3.visible, btn_finish3.visible = False, True
                            (btn_finish3.enable() if ok else btn_finish3.disable())

                    code.on_value_change(lambda e: (_apply_mask_for(e.value), _sync_phone()))
                    number.on_value_change(_sync_phone)

                    _apply_mask_for(DEFAULT_CODE)

                    _sync_phone()

                    # ВАЖНО: объявляем хэндлер ДО привязки к кнопке
                    async def _finish_passenger(e):
                        if finish_lock.locked():
                            await log_info(f"[finish_passenger] Параллельная попытка для uid={uid}", type_msg="warning")
                            return
                        async with finish_lock:
                            await log_info(f"[finish_passenger] Начало регистрации uid={uid}", type_msg="info")
                            
                            try:
                                data = {
                                    'role': 'passenger',
                                    'country': model['country'],
                                    'region': model['region'],
                                    'city': model['city'],
                                    'phone_passenger': model['phone_passenger'],
                                    'phone_driver': model['phone_passenger']
                                }
                                
                                if await user_exists(uid):
                                    await update_table('users', uid, data)
                                    await log_info(f"[finish_passenger] Обновлён пассажир uid={uid}", type_msg="info")
                                else:
                                    data['user_id'] = uid
                                    await insert_into_table('users', data)
                                    await log_info(f"[finish_passenger] Создан новый пассажир uid={uid}", type_msg="info")
                                
                                # Отправка уведомления
                                await send_info_msg(
                                    text=f'Тип сообщения: Инфо\nНовый пассажир!\n'
                                        f'Username: {user_data.get("username") if user_data else "—"}\n'
                                        f'First name: {user_data.get("first_name") if user_data else "—"}\n'
                                        f'User ID: {uid}\n'
                                        f'Country: {data["country"]}\n'
                                        f'Region: {data["region"]}\n'
                                        f'City: {data["city"]}\n'
                                        f'Phone: {data["phone_passenger"]}\n',
                                    type_msg_tg="new_users"
                                )
                                
                                await log_info(f"[finish_passenger] Успешно завершено для uid={uid}", type_msg="info")
                                await back_button.deactivate()
                                ui.navigate.to('/main_app?tab=main')
                                
                            except Exception as ex:
                                await log_info(f"[finish_passenger][ОШИБКА] uid={uid} | {ex!r}", type_msg="error")
                                ui.notify(
                                    lang_dict('error_registration', user_lang),
                                    type='negative',
                                    position='center'
                                )
                step_phone.visible = False


                # 4) Водительское удостоверение (только для водителя)
                with ui.step(lang_dict('step_driver_license', user_lang)).props('name=driver_license').classes('w-full q-pa-none q-ma-none') as step_driver_license:
                    license_ok = {'img': False}

                    async def _submit_license(_):
                        if license_ok['img']:
                            await _step_next('driver_license')
                        else:
                            await ui.run_javascript("document.activeElement && document.activeElement.blur()")

                    with ui.element('div').classes('w-full q-gutter-y-md'):
                        ui.label(lang_dict('upload_driver_license_hint', user_lang)).classes('text-sm text-warning')
                        
                        with ui.row().classes('items-center w-full q-gutter-y-md gap-2'):
                            up_license = ui.upload(label=lang_dict('upload_driver_license_label', user_lang)).props('accept="image/*" auto-upload').classes('w-full')

                        with ui.stepper_navigation().classes('w-full q-gutter-y-md q-pa-none q-ma-none'):
                            next_license = ui.button(lang_dict('btn_next', user_lang), on_click=_submit_license)
                            next_license.disable()

                    async def on_license_upload(e):
                        try:
                            model['driver_license'] = await _save_upload(
                                uid,
                                e,
                                'driver_license',
                                None,
                                lang=user_lang,
                            )
                            license_ok['img'] = True
                            ui.notify(lang_dict('upload_driver_license_success', user_lang))
                            next_license.enable()
                        except Exception as ex:
                            license_ok['img'] = False
                            next_license.disable()
                            ui.notify(f'{lang_dict("upload_error_prefix", user_lang)}: {ex}', type='negative')

                    up_license.on_upload(on_license_upload)
                step_driver_license.visible = False


                # 5) Авто (только для водителя)
                with ui.step(lang_dict('step_car', user_lang)).props('name=car').classes('w-full q-pa-none q-ma-none') as step_car:
                    car_ok = {'img': False}

                    async def _submit_car(_):
                        brand_ok = bool((car_brand.value or '').strip())
                        model_ok = bool((car_model.value or '').strip())
                        color_ok = bool((car_color.value or '').strip())

                        async def _focus(el):
                            await ui.run_javascript(f"getHtmlElement({el.id})?.querySelector('input')?.focus()")

                        if brand_ok and not model_ok:
                            await _focus(car_model); return
                        if model_ok and not color_ok:
                            await _focus(car_color); return
                        if model.get('car_brand') and model.get('car_model') and model.get('car_color') and car_ok['img']:
                            await _step_next('car')

                    with ui.element('div').classes('w-full q-gutter-y-md gap-2'):
                        car_brand = ui.input(lang_dict('car_brand_label', user_lang)).props('enterkeyhint=next').classes('w-full')
                        car_model = ui.input(lang_dict('car_model_label', user_lang)).props('enterkeyhint=next').classes('w-full')
                        car_color = ui.input(lang_dict('car_color_label', user_lang)).props('enterkeyhint=done').classes('w-full')

                        # Подсказки на клавиатуре
                        ui.run_javascript(f"""
                        getHtmlElement({car_brand.id})?.querySelector('input')?.setAttribute('enterkeyhint','next');
                        getHtmlElement({car_model.id})?.querySelector('input')?.setAttribute('enterkeyhint','next');
                        getHtmlElement({car_color.id})?.querySelector('input')?.setAttribute('enterkeyhint','done');
                        """)

                        with ui.row().classes('items-center w-full q-gutter-y-md gap-2'):
                            up_car = ui.upload(label=lang_dict('upload_car_label', user_lang)).props('accept="image/*" auto-upload').classes('w-full')

                        with ui.stepper_navigation().classes('w-full q-gutter-y-md q-pa-none q-ma-none'):
                            async def _on_car_next(_):
                                await _step_next('car')

                            next4 = ui.button(lang_dict('btn_next', user_lang), on_click=_on_car_next)

                        def _sync_car(*_):
                            model['car_brand'] = (car_brand.value or '').strip()
                            model['car_model'] = (car_model.value or '').strip()
                            model['car_color'] = (car_color.value or '').strip()
                            ready = bool(model['car_brand'] and model['car_model'] and model['car_color'] and car_ok['img'])
                            (next4.enable() if ready else next4.disable())
                        car_brand.on_value_change(_sync_car); car_model.on_value_change(_sync_car); car_color.on_value_change(_sync_car)

                        async def on_car_upload(e):
                            try:
                                model['car_image'] = await _save_upload(
                                    uid,
                                    e,
                                    'car',
                                    None,
                                    lang=user_lang,
                                )
                                car_ok['img'] = True
                                ui.notify(lang_dict('upload_car_success', user_lang))
                                _sync_car()
                            except Exception as ex:
                                ui.notify(f'{lang_dict("upload_error_prefix", user_lang)}: {ex}', type='negative')

                        up_car.on_upload(on_car_upload)
                step_car.visible = False


                # 6) Госномер + техпаспорт (водитель)
                with ui.step(lang_dict('step_docs', user_lang)).props('name=docs').classes('w-full q-pa-none q-ma-none') as step_docs:
                    tp_ok = {'img': False}

                    async def _submit_docs(_):
                        if (plate.value or '').strip() and tp_ok['img']:
                            await _finish_driver()
                        else:
                            # нет данных — просто закрыть клавиатуру
                            await ui.run_javascript("document.activeElement && document.activeElement.blur()")

                    with ui.element('div').classes('w-full q-gutter-y-md'):
                        plate = ui.input(lang_dict('plate_label', user_lang)).props('enterkeyhint=done')
                        ui.run_javascript(f"getHtmlElement({plate.id})?.querySelector('input')?.setAttribute('enterkeyhint','done')")
                        
                        with ui.row().classes('items-center w-full q-gutter-y-md gap-2'):
                            up_tp = ui.upload(label=lang_dict('upload_techpass_label', user_lang)).props('accept="image/*,application/pdf" auto-upload').classes('w-full')

                        with ui.stepper_navigation().classes('w-full q-gutter-y-md q-pa-none q-ma-none'):
                            finish5 = ui.button(lang_dict('btn_finish', user_lang), on_click=_submit_docs) 

                    def _sync_docs(*_):
                        model['car_number'] = (plate.value or '').strip().upper()
                        ready = bool(model['car_number'] and tp_ok['img'])
                        (finish5.enable() if ready else finish5.disable())
                    plate.on_value_change(_sync_docs)

                    async def on_tp_upload(e):
                        try:
                            model['techpass_image'] = await _save_upload(
                                uid,
                                e,
                                'techpass',
                                None,
                                lang=user_lang,
                            )
                            tp_ok['img'] = True
                            ui.notify(lang_dict('upload_techpass_success', user_lang))
                            _sync_docs()                      # <<< ключевая строка
                        except Exception as ex:
                            tp_ok['img'] = False              # на всякий случай — состояние консистентно
                            _sync_docs()                      # пересчитать доступность кнопки
                            ui.notify(f'{lang_dict("upload_error_prefix", user_lang)}: {ex}', type='negative')

                    async def _finish_driver():
                        if finish_lock.locked():
                            return
                        async with finish_lock:
                            data = {
                                'role': 'driver',
                                'country': model['country'], 'region': model['region'], 'city': model['city'],
                                'phone_driver': model['phone_driver'], 'phone_passenger': model['phone_driver'],
                                'car_brand': model['car_brand'], 'car_model': model['car_model'], 'car_color': model['car_color'], 'car_number': model['car_number'],
                                'car_image': model['car_image'], 'techpass_image': model['techpass_image'], 'driver_license': model['driver_license'],
                            }
                            if await user_exists(uid):
                                await update_table('users', uid, data)
                            else:
                                data['user_id'] = uid
                                await insert_into_table('users', data)
                            caption = (
                                "Тип сообщения: Инфо\n"
                                "Новый водитель!\n"
                                f'Username: {user_data.get("username") if user_data else "—"}\n'
                                f'First name: {user_data.get("first_name") if user_data else "—"}\n'
                                f'User ID: {uid}\n'
                                f'Country: {data["country"]}\n'
                                f'Region: {data["region"]}\n'
                                f'City: {data["city"]}\n'
                                f'Phone: {data["phone_driver"]}\n'
                                f'Car: {data["car_brand"]} {data["car_model"]} {data["car_color"]} {data["car_number"]}'
                            )

                            ui.notify(lang_dict('notify_verification', user_lang), type='warning', position='center')
                            await log_info(f"[verify_driver] notify_user uid={uid}", type_msg="info")
                            await back_button.deactivate()
                            ui.timer(5.0, lambda: ui.navigate.to('/main_app?tab=main'), once=True)

                            await send_info_msg(photo=[model['car_image'], model['techpass_image'], model['driver_license']], type_msg_tg="new_users", caption=f"Документы водителя {uid}")
                            await send_info_msg(text=caption, type_msg_tg="new_users", reply_markup=verification_inline_kb())
                            return
            
                    up_tp.on_upload(on_tp_upload)
                
                step_docs.visible = False

                # Фиксируем порядок шагов для Telegram BackButton.
                step_sequence[:] = ([] if not choice_role else ['role']) + ['city', 'phone', 'driver_license', 'car', 'docs']
                step_indexes.clear()
                step_indexes.update({name: idx for idx, name in enumerate(step_sequence)})

            def _apply_flow_visibility() -> None:
                """Обновляет доступность шагов после выбора роли."""
                is_driver = (model.get('role') == 'driver')
                step_city.visible = True
                step_phone.visible = True
                step_driver_license.visible = is_driver
                step_car.visible = is_driver
                step_docs.visible = is_driver

            if choice_role:
                async def _on_role_next(_):
                    _apply_flow_visibility()
                    await _step_next('role')

                next1.on_click(_on_role_next)
                st.set_value('role')
            else:
                # Автоматически раскрываем шаги для водителя и переходим к городу
                _apply_flow_visibility()
                st.set_value('city')

            initial_step = 'role' if choice_role else 'city'
            await _sync_back_button(initial_step)

            async def _on_step_change(e):
                val = getattr(e, 'value', None)
                await _sync_back_button(val)
                if val == 'car':
                    await bind_enter_action(car_brand, dst=car_model)
                    await bind_enter_action(car_model, dst=car_color)
                    await bind_enter_action(car_color, close=True)
                elif val == 'docs':
                    await bind_enter_action(plate, close=True)

            st.on_value_change(_on_step_change)