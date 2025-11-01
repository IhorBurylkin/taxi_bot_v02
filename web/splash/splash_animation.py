"""
Splash Screen Animation для NiceGUI Telegram WebApp

ИСПРАВЛЕННАЯ версия - правильно работает с декораторами require_twa и with_theme_toggle

Ключевые изменения:
1. Splash показывается СРАЗУ, не дожидаясь theme:ready
2. Основная функция страницы выполняется ПАРАЛЛЕЛЬНО с показом splash
3. Splash скрывается после завершения инициализации и минимального времени показа

Использование:
    @ui.page('/main_app')
    @splash_screen(svg_path=None, duration=2000)  # Внешний декоратор
    @require_twa                                   # Средний декоратор
    @with_theme_toggle                             # Внутренний декоратор
    async def main_app():
        # ваш код страницы
        pass
"""

from __future__ import annotations
import asyncio
from functools import wraps
from pathlib import Path
from urllib.parse import quote as url_quote
from nicegui import ui
from web.web_utilits import _safe_js


def _get_default_svg() -> str:
    """Дефолтный SVG с анимацией для SPAR-TAXI (адаптирован для веба/мобилок)"""
    return '''<svg xmlns="http://www.w3.org/2000/svg"
     viewBox="0 0 400 600"
     preserveAspectRatio="xMidYMid slice"
     role="img" aria-label="SPAR-TAXI loading"
     style="width:100vw;height:100svh;display:block">

  <defs>
    <!-- Градиент фона -->
    <linearGradient id="bgGradient" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#0057B7"/>
      <stop offset="100%" stop-color="#FFD500"/>
    </linearGradient>

    <!-- Свечение (умеренное значение для мобильной производительности) -->
    <filter id="glow" filterUnits="objectBoundingBox">
      <feGaussianBlur stdDeviation="3" result="blur"/>
      <feMerge>
        <feMergeNode in="blur"/>
        <feMergeNode in="SourceGraphic"/>
      </feMerge>
    </filter>

    <!-- Анимации -->
    <style>
      /* ВАЖНО: трансформации для SVG через CSS */
      .tbox { transform-box: fill-box; transform-origin: center; }

      @keyframes fadeIn {
        from { opacity: 0; transform: scale(0.9); }
        to   { opacity: 1; transform: scale(1); }
      }
      @keyframes pulse {
        0%,100% { opacity: .6; }
        50%     { opacity: 1; }
      }
      @keyframes rotate {
        to { transform: rotate(360deg); }
      }
      @keyframes dotBounce {
        0%,80%,100% { transform: translateY(0); }
        40%         { transform: translateY(-10px); }
      }

      .logo    { animation: fadeIn .8s ease-out forwards; }
      .spinner { animation: rotate 2s linear infinite; }
      .pulse   { animation: pulse 2s ease-in-out infinite; }
      .dot     { animation: dotBounce 1.4s ease-in-out infinite; }

      .dot:nth-child(2) { animation-delay: .2s; }
      .dot:nth-child(3) { animation-delay: .4s; }

      /* Системные шрифты с поддержкой латиницы/кириллицы */
      text { font-family: system-ui, -apple-system, "Segoe UI", Roboto, Arial, "Noto Sans", "Liberation Sans", sans-serif; }
    </style>
  </defs>

  <!-- Фон -->
  <rect width="100%" height="100%" fill="url(#bgGradient)"/>

  <!-- Центральная группа -->
  <g class="logo tbox">
    <!-- Центральный блок -->
    <g transform="translate(200, 250)">
      <!-- Круг со спиннером -->
      <circle cx="0" cy="0" r="60" fill="rgba(255,255,255,0.1)" stroke="white" stroke-width="2" class="pulse"/>

      <!-- «Такси»-бейдж -->
      <g filter="url(#glow)" class="tbox">
        <rect x="-30" y="-20" width="60" height="40" rx="8" fill="#1a1a1a"/>
        <rect x="-25" y="-15" width="50" height="30" rx="4" fill="#FFD500"/>
        <text x="0" y="5" text-anchor="middle" font-size="20" font-weight="700" fill="#1a1a1a">TAXI</text>
      </g>
    </g>

    <!-- Название -->
    <text x="200" y="360" text-anchor="middle" font-size="32" font-weight="700" fill="#fff" filter="url(#glow)">
      SPAR-TAXI
    </text>

    <!-- Подзаголовок -->
    <text x="200" y="390" text-anchor="middle" font-size="14" fill="#fff" opacity="0.9">
      für Ukrainer
    </text>

    <!-- Українська фраза -->
    <text x="200" y="415" text-anchor="middle" font-size="13" fill="#fff" opacity="0.85" font-style="italic">
      Таксі, що долає від серця до серця.
    </text>

    <!-- Точки загрузки -->
    <g transform="translate(200, 440)">
      <circle class="dot tbox" cx="-20" cy="0" r="4" fill="#fff"/>
      <circle class="dot tbox" cx="0"   cy="0" r="4" fill="#fff"/>
      <circle class="dot tbox" cx="20"  cy="0" r="4" fill="#fff"/>
    </g>
  </g>
</svg>'''



def _load_svg(svg_path: str | None) -> str:
    """Загружает SVG из файла или возвращает дефолтный"""
    if svg_path:
        try:
            path = Path(svg_path)
            if path.exists() and path.is_file():
                print(f"[SPLASH] Загружаю SVG из файла: {svg_path}")
                return path.read_text(encoding='utf-8')
            else:
                print(f"[SPLASH] Файл не найден: {svg_path}, использую дефолтный SVG")
        except Exception as e:
            print(f"[SPLASH] Ошибка загрузки SVG: {e}")
    else:
        print("[SPLASH] svg_path=None, использую дефолтный SVG")
    
    return _get_default_svg()


async def show_splash_immediate(
    svg_content: str,
    fade_in_ms: int = 300,
    fade_out_ms: int = 500,
    z_index: int = 2147483000
) -> tuple:
    """
    Показывает splash screen СРАЗУ, не дожидаясь theme:ready
    
    КЛЮЧЕВОЕ ОТЛИЧИЕ: не ждет события theme:ready, показывает splash немедленно
    """
    
    import uuid
    overlay_id = f'splash-{uuid.uuid4().hex[:8]}'
    
    svg_data_url = 'data:image/svg+xml;charset=utf-8,' + url_quote(svg_content, safe='')
    
    # Добавляем overlay с максимальным приоритетом
    ui.add_body_html(f'''
        <div id="{overlay_id}" style="
            position: fixed !important;
            top: 0 !important;
            left: 0 !important;
            right: 0 !important;
            bottom: 0 !important;
            width: 100vw !important;
            height: 100vh !important;
            height: 100svh !important;
            height: 100dvh !important;
            z-index: {z_index} !important;
            background: linear-gradient(135deg, #0057B7 0%, #FFD500 100%) !important;
            display: flex !important;
            align-items: center !important;
            justify-content: center !important;
            opacity: 0;
            visibility: visible !important;
            transition: opacity {fade_in_ms}ms ease-in-out;
            overflow: hidden !important;
            pointer-events: auto !important;
        ">
            <div style="
                width: 100%;
                height: 100%;
                background-image: url('{svg_data_url}');
                background-size: cover;
                background-position: center;
                background-repeat: no-repeat;
            "></div>
        </div>
    ''')
    
    # УПРОЩЕННЫЙ JavaScript - показываем СРАЗУ, без ожидания theme:ready
    await _safe_js(f'''
        (async function() {{
            console.log('[SPLASH] Немедленная инициализация splash screen ID: {overlay_id}');
            
            const overlay = document.getElementById('{overlay_id}');
            if (!overlay) {{
                console.error('[SPLASH] Overlay не найден!');
                return;
            }}
            
            console.log('[SPLASH] Overlay найден, запрещаем скролл');
            // Запрещаем скролл
            document.body.style.overflow = 'hidden';
            
            // Устанавливаем CSS-переменные для viewport height
            function setViewportHeight() {{
                const vh = window.innerHeight * 0.01;
                document.documentElement.style.setProperty('--vh', `${{vh}}px`);
            }}
            setViewportHeight();
            window.addEventListener('resize', setViewportHeight);
            
            // КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: показываем СРАЗУ, без ожидания
            console.log('[SPLASH] Показываем splash немедленно...');
            await new Promise(r => setTimeout(r, 10));  // Минимальная задержка для отрисовки DOM
            overlay.style.opacity = '1';
            console.log('[SPLASH] Splash отображён, opacity=1');
        }})();
    ''', timeout=3.0)
    
    async def hide():
        """Скрывает splash screen"""
        try:
            print(f"[SPLASH] Начинаем скрывать splash {overlay_id}")
            
            await _safe_js(f'''
                console.log('[SPLASH] Скрываем overlay...');
                const overlay = document.getElementById('{overlay_id}');
                if (overlay) {{
                    overlay.style.opacity = '0';
                    overlay.style.pointerEvents = 'none';
                    console.log('[SPLASH] Opacity установлена в 0');
                }} else {{
                    console.error('[SPLASH] Overlay не найден при скрытии');
                }}
            ''', timeout=3.0)
            
            # Ждём завершения анимации
            await asyncio.sleep(fade_out_ms / 1000 + 0.1)
            
            await _safe_js(f'''
                console.log('[SPLASH] Удаляем overlay и возвращаем скролл');
                document.body.style.overflow = '';
                const overlay = document.getElementById('{overlay_id}');
                if (overlay) {{
                    overlay.remove();
                    console.log('[SPLASH] Overlay удалён');
                }}
            ''', timeout=3.0)
            
            print(f"[SPLASH] Splash {overlay_id} успешно скрыт и удалён")
            
        except Exception as e:
            print(f"[SPLASH] Ошибка при скрытии splash: {e}")
    
    return overlay_id, hide


def splash_screen(
    svg_path: str | None = None,
    duration: int = 2000,
    fade_in: int = 300,
    fade_out: int = 500,
    auto_hide: bool = True
):
    """
    ИСПРАВЛЕННЫЙ декоратор для добавления splash screen к странице NiceGUI
    
    Правильно работает с декораторами require_twa и with_theme_toggle:
    - Показывает splash СРАЗУ
    - Запускает основную функцию ПАРАЛЛЕЛЬНО (которая инициализирует theme boot)
    - Скрывает splash после минимального времени показа
    
    Args:
        svg_path: Путь к SVG файлу (если None - используется дефолтный)
        duration: Минимальное время показа splash (мс)
        fade_in: Время появления (мс)
        fade_out: Время исчезновения (мс)
        auto_hide: Автоматически скрывать после загрузки страницы
        
    Правильный порядок декораторов:
        @ui.page('/main_app')
        @splash_screen(duration=2000)    # Внешний - выполняется последним
        @require_twa                      # Средний
        @with_theme_toggle                # Внутренний - выполняется первым
        async def main_app():
            ...
    """
    
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            print(f"[SPLASH] Декоратор splash_screen вызван для {func.__name__}")
            print(f"[SPLASH] Параметры: svg_path={svg_path}, duration={duration}, fade_in={fade_in}, fade_out={fade_out}")
            
            # 1. Загружаем SVG
            svg_content = _load_svg(svg_path)
            print(f"[SPLASH] SVG загружен, длина: {len(svg_content)} символов")
            
            # 2. Показываем splash НЕМЕДЛЕННО
            print("[SPLASH] Показываем splash немедленно...")
            overlay_id, hide = await show_splash_immediate(
                svg_content=svg_content,
                fade_in_ms=fade_in,
                fade_out_ms=fade_out
            )
            print(f"[SPLASH] Splash показан, overlay_id={overlay_id}")
            
            # 3. Засекаем время начала
            import time
            start_time = time.time()
            
            try:
                # 4. ПАРАЛЛЕЛЬНО запускаем основную функцию страницы
                #    (она выполнит require_twa, with_theme_toggle и остальную инициализацию)
                print(f"[SPLASH] Запускаем основную функцию {func.__name__} параллельно...")
                result = await func(*args, **kwargs)
                print(f"[SPLASH] Основная функция {func.__name__} завершена")
                
                if auto_hide:
                    # 5. Вычисляем оставшееся время до минимального duration
                    elapsed_ms = (time.time() - start_time) * 1000
                    remaining_ms = max(0, duration - elapsed_ms)
                    
                    if remaining_ms > 0:
                        print(f"[SPLASH] Ждём ещё {remaining_ms:.0f}ms до минимального времени показа...")
                        await asyncio.sleep(remaining_ms / 1000)
                    
                    # 6. Скрываем splash
                    print("[SPLASH] Скрываем splash...")
                    await hide()
                    print("[SPLASH] Splash скрыт")
                
                return result
                
            except Exception as e:
                # В случае ошибки тоже скрываем splash
                print(f"[SPLASH] ОШИБКА в основной функции: {e}")
                if auto_hide:
                    await hide()
                raise
        
        return wrapper
    return decorator


# ============================================================================
# ПРИМЕР ИСПОЛЬЗОВАНИЯ
# ============================================================================

if __name__ == '__main__':
    """
    Демонстрация правильного порядка декораторов
    """
    
    # Симуляция декораторов require_twa и with_theme_toggle
    def mock_require_twa(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            print("[MOCK] require_twa: инициализация TWA...")
            await asyncio.sleep(0.3)  # Симуляция инициализации
            result = await fn(*args, **kwargs)
            print("[MOCK] require_twa: завершено")
            return result
        return wrapper
    
    def mock_with_theme_toggle(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            print("[MOCK] with_theme_toggle: добавление переключателя темы...")
            await asyncio.sleep(0.2)  # Симуляция добавления UI
            result = await fn(*args, **kwargs)
            print("[MOCK] with_theme_toggle: завершено")
            return result
        return wrapper
    
    # Правильный порядок
    @ui.page('/')
    @splash_screen(svg_path=None, duration=2000, fade_in=300, fade_out=500)
    @mock_require_twa
    @mock_with_theme_toggle
    async def index():
        print("[PAGE] Рендеринг основной страницы...")
        await asyncio.sleep(0.5)  # Симуляция загрузки данных
        
        ui.label('Главная страница SPAR-TAXI').classes('text-h4')
        ui.label('Добро пожаловать!').classes('text-subtitle1')
        
        with ui.card():
            ui.label('Контент загружен')
        
        print("[PAGE] Рендеринг завершен")
    
    print("\n" + "="*80)
    print("ДЕМОНСТРАЦИЯ: Правильный порядок декораторов")
    print("="*80)
    print("\nПорядок выполнения (изнутри наружу):")
    print("1. mock_with_theme_toggle - выполняется первым")
    print("2. mock_require_twa - выполняется вторым")
    print("3. splash_screen - выполняется последним")
    print("\nРезультат:")
    print("- Splash показывается СРАЗУ")
    print("- Инициализация идет ПАРАЛЛЕЛЬНО")
    print("- Splash скрывается после минимального времени")
    print("="*80 + "\n")
    
    ui.run(port=8080)