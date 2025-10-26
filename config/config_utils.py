from typing import Optional
from config.config import SUPPORTED_LANGUAGES, DEFAULT_LANGUAGES, MESSAGES  # MESSAGES = lang_dict["MESSAGES"]

def resolve_lang(user_lang: Optional[str]) -> str:
    """Возвращает поддерживаемый язык или DEFAULT_LANGUAGES."""
    supported = (SUPPORTED_LANGUAGES or [])
    default = (DEFAULT_LANGUAGES or "en").lower()
    lang = (user_lang or "").lower()
    return lang if lang in supported else default

def lang_dict(key: str, user_lang: Optional[str] = None, **fmt) -> str:
    """Достаёт перевод по ключу с фолбэком на DEFAULT_LANGUAGES. Поддерживает .format()."""
    lang = resolve_lang(user_lang)
    default = (DEFAULT_LANGUAGES or "en").lower()
    val = (
        (MESSAGES or {}).get(lang, {}).get(key)
        or (MESSAGES or {}).get(default, {}).get(key)
        or key
    )
    try:
        return val.format(**fmt) if fmt else val
    except Exception:
        return val
