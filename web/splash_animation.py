"""
Splash Screen Animation для NiceGUI Telegram WebApp

Полноэкранный splash screen для красивой загрузки приложения.

Возможности:
- Плавные анимации появления/исчезновения
- Поддержка пользовательских SVG
- Адаптивная высота для всех мобильных устройств
- Минимальное время показа (чтобы пользователь успел увидеть логотип)

Примечание: Для открытия webapp на весь экран используйте ваш существующий код
с Telegram.WebApp.expand() в основной функции страницы.

Использование:

@splash_screen(svg_path='/path/to/logo.svg', duration=2000)
async def my_page():
    # ваш код страницы
    pass
"""

from __future__ import annotations
import asyncio
from functools import wraps
from pathlib import Path
from urllib.parse import quote as url_quote
from nicegui import ui


def _get_default_svg() -> str:
    """Дефолтный SVG с анимацией для SPAR-TAXI"""
    return '''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     viewBox="0 0 400 600"
     width="100%" height="100%"
     preserveAspectRatio="xMidYMid meet">
  
  <defs>
    <!-- Градиент фона -->
    <linearGradient id="bgGradient" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#0057B7"/>
      <stop offset="100%" stop-color="#FFD500"/>
    </linearGradient>
    
    <!-- Свечение -->
    <filter id="glow">
      <feGaussianBlur stdDeviation="4" result="blur"/>
      <feMerge>
        <feMergeNode in="blur"/>
        <feMergeNode in="SourceGraphic"/>
      </feMerge>
    </filter>
    
    <!-- Анимации -->
    <style>
      @keyframes fadeIn {
        from { opacity: 0; transform: scale(0.8); }
        to { opacity: 1; transform: scale(1); }
      }
      
      @keyframes pulse {
        0%, 100% { opacity: 0.6; }
        50% { opacity: 1; }
      }
      
      @keyframes rotate {
        from { transform: rotate(0deg); }
        to { transform: rotate(360deg); }
      }
      
      @keyframes dotBounce {
        0%, 80%, 100% { transform: translateY(0); }
        40% { transform: translateY(-10px); }
      }
      
      .logo {
        animation: fadeIn 0.8s ease-out forwards;
        transform-origin: center;
      }
      
      .spinner {
        animation: rotate 2s linear infinite;
        transform-origin: center;
      }
      
      .pulse {
        animation: pulse 2s ease-in-out infinite;
      }
      
      .dot {
        animation: dotBounce 1.4s ease-in-out infinite;
      }
      
      .dot:nth-child(2) { animation-delay: 0.2s; }
      .dot:nth-child(3) { animation-delay: 0.4s; }
    </style>
  </defs>
  
  <!-- Фон -->
  <rect width="100%" height="100%" fill="url(#bgGradient)"/>
  
  <!-- Центральная группа -->
  <g class="logo">
    <!-- Основной логотип/текст -->
    <g transform="translate(200, 250)">
      <!-- Круг со спиннером -->
      <circle cx="0" cy="0" r="60" fill="rgba(255,255,255,0.1)" stroke="white" 
              stroke-width="2" class="pulse"/>
      
      <g class="spinner">
        <circle cx="0" cy="-50" r="8" fill="white"/>
        <circle cx="35" cy="-35" r="6" fill="white" opacity="0.8"/>
        <circle cx="50" cy="0" r="4" fill="white" opacity="0.6"/>
        <circle cx="35" cy="35" r="6" fill="white" opacity="0.4"/>
      </g>
      
      <!-- Логотип такси -->
      <g filter="url(#glow)">
        <rect x="-30" y="-20" width="60" height="40" rx="8" fill="#1a1a1a"/>
        <rect x="-25" y="-15" width="50" height="30" rx="4" fill="#FFD500"/>
        <text x="0" y="5" text-anchor="middle" font-size="20" 
              font-weight="bold" fill="#1a1a1a" font-family="Arial">
          TAXI
        </text>
      </g>
    </g>
    
    <!-- Название -->
    <text x="200" y="360" text-anchor="middle" font-size="32" 
          font-weight="bold" fill="white" font-family="Arial" filter="url(#glow)">
      SPAR-TAXI
    </text>
    
    <!-- Подзаголовок -->
    <text x="200" y="390" text-anchor="middle" font-size="14" 
          fill="white" opacity="0.9" font-family="Arial">
      für Ukrainer
    </text>
    
    <!-- Анимированные точки загрузки -->
    <g transform="translate(200, 420)">
      <circle class="dot" cx="-20" cy="0" r="4" fill="white"/>
      <circle class="dot" cx="0" cy="0" r="4" fill="white"/>
      <circle class="dot" cx="20" cy="0" r="4" fill="white"/>
    </g>
    
    <!-- Текст загрузки -->
    <text x="200" y="460" text-anchor="middle" font-size="12" 
          fill="white" opacity="0.8" font-family="Arial">
      Завантаження...
    </text>
  </g>
</svg>'''


def _load_svg(svg_path: str | None) -> str:
    """Загружает SVG из файла или возвращает дефолтный"""
    if svg_path:
        try:
            path = Path(svg_path)
            if path.exists() and path.is_file():
                return path.read_text(encoding='utf-8')
        except Exception as e:
            print(f"Ошибка загрузки SVG: {e}")
    
    return _get_default_svg()


async def show_splash(
    svg_content: str,
    fade_in_ms: int = 300,
    fade_out_ms: int = 500,
    z_index: int = 9999
) -> tuple:
    """
    Показывает splash screen на весь экран
    
    Args:
        svg_content: SVG код
        fade_in_ms: Время появления (мс)
        fade_out_ms: Время исчезновения (мс)
        z_index: Z-индекс overlay
        
    Returns:
        (overlay_element, hide_function)
    """
    
    # Добавляем viewport meta-тег для мобильных устройств (если еще не добавлен)
    ui.add_head_html('''
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    ''')
    
    # Создаём data URL для SVG
    svg_data_url = 'data:image/svg+xml;charset=utf-8,' + url_quote(svg_content, safe='')
    
    # Создаём уникальный ID для overlay
    import uuid
    overlay_id = f'splash-{uuid.uuid4().hex[:8]}'
    
    # Создаём overlay через HTML напрямую, чтобы избежать проблем с парсингом NiceGUI
    ui.add_body_html(f'''
        <div id="{overlay_id}" style="
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            width: 100vw;
            height: 100vh;
            height: calc(var(--vh, 1vh) * 100);
            height: 100svh;
            height: 100dvh;
            z-index: {z_index};
            background: linear-gradient(135deg, #0057B7 0%, #FFD500 100%);
            display: flex;
            align-items: center;
            justify-content: center;
            opacity: 0;
            transition: opacity {fade_in_ms}ms ease-in-out;
            overflow: hidden;
        ">
            <div style="
                width: 100%;
                height: 100%;
                background-image: url({svg_data_url});
                background-size: contain;
                background-position: center;
                background-repeat: no-repeat;
            "></div>
        </div>
    ''')
    
    # Дополнительные настройки через JS
    await ui.run_javascript(f'''
        (function() {{
            const overlay = document.getElementById('{overlay_id}');
            if (!overlay) return;
            
            // Запрещаем скролл на время показа splash
            document.body.style.overflow = 'hidden';
            
            // Устанавливаем CSS-переменные для viewport height (для мобильных)
            function setViewportHeight() {{
                const vh = window.innerHeight * 0.01;
                document.documentElement.style.setProperty('--vh', `${{vh}}px`);
            }}
            setViewportHeight();
            window.addEventListener('resize', setViewportHeight);
            
            // Плавное появление
            setTimeout(() => {{
                overlay.style.opacity = '1';
            }}, 50);
        }})();
    ''')
    
    async def hide():
        """Скрывает splash screen"""
        try:
            # Плавное исчезновение
            await ui.run_javascript(f'''
                const overlay = document.getElementById('{overlay_id}');
                if (overlay) {{
                    overlay.style.opacity = '0';
                    overlay.style.pointerEvents = 'none';
                }}
            ''')
            
            # Ждём завершения анимации
            await asyncio.sleep(fade_out_ms / 1000 + 0.1)
            
            # Возвращаем скролл и удаляем элемент
            await ui.run_javascript(f'''
                document.body.style.overflow = '';
                const overlay = document.getElementById('{overlay_id}');
                if (overlay) {{
                    overlay.remove();
                }}
            ''')
            
        except Exception as e:
            print(f"Ошибка при скрытии splash: {e}")
    
    return overlay_id, hide


def splash_screen(
    svg_path: str | None = None,
    duration: int = 2000,
    fade_in: int = 300,
    fade_out: int = 500,
    auto_hide: bool = True
):
    """
    Декоратор для добавления splash screen к странице NiceGUI
    
    Args:
        svg_path: Путь к SVG файлу (если None - используется дефолтный)
        duration: Минимальное время показа splash (мс)
        fade_in: Время появления (мс)
        fade_out: Время исчезновения (мс)
        auto_hide: Автоматически скрывать после загрузки страницы
        
    Пример использования:
        @ui.page('/')
        @splash_screen(svg_path='./logo.svg', duration=2000)
        async def index():
            ui.label('Главная страница')
    """
    
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Загружаем SVG
            svg_content = _load_svg(svg_path)
            
            # Показываем splash
            overlay_id, hide = await show_splash(
                svg_content=svg_content,
                fade_in_ms=fade_in,
                fade_out_ms=fade_out
            )
            
            try:
                # Запускаем основную функцию страницы
                result = await func(*args, **kwargs)
                
                if auto_hide:
                    # Ждём минимальное время показа
                    await asyncio.sleep(duration / 1000)
                    
                    # Скрываем splash
                    await hide()
                
                return result
                
            except Exception as e:
                # В случае ошибки тоже скрываем splash
                if auto_hide:
                    await hide()
                raise e
        
        return wrapper
    return decorator


# ============================================================================
# ПРИМЕР ИСПОЛЬЗОВАНИЯ
# ============================================================================

if __name__ == '__main__':
    from nicegui import app
    
    # Пример 1: Базовое использование с дефолтным SVG
    @ui.page('/')
    @splash_screen(duration=2000)
    async def index():
        # Ваш код для открытия webapp на весь экран (если нужно)
        await ui.run_javascript('''
            try{
                const tg = window.Telegram?.WebApp;
                tg?.ready?.();
                tg?.expand?.();
                tg?.disableVerticalSwipes?.();
            }catch(e){}
        ''', timeout=3.0)
        
        ui.label('Главная страница SPAR-TAXI').classes('text-h4')
        ui.label('Добро пожаловать!').classes('text-subtitle1')
        
        with ui.card():
            ui.label('Контент загружен')
    
    
    # Пример 2: С пользовательским SVG
    @ui.page('/custom')
    @splash_screen(
        svg_path='/path/to/custom.svg',
        duration=2000,
        fade_in=500,
        fade_out=800
    )
    async def custom_page():
        ui.label('Страница с кастомным splash').classes('text-h4')
    
    
    # Пример 3: Интеграция с существующими декораторами
    # @ui.page('/main_app')
    # @splash_screen(svg_path='/mnt/data/spar_taxi_splash.svg', duration=2000)
    # @require_twa
    # @with_theme_toggle
    # async def main_app():
    #     await ui.run_javascript('''
    #         try{
    #             const tg = window.Telegram?.WebApp;
    #             tg?.ready?.();
    #             tg?.expand?.();
    #             tg?.disableVerticalSwipes?.();
    #         }catch(e){}
    #     ''', timeout=3.0)
    #     
    #     # ваш код страницы...
    
    
    ui.run(port=8080)