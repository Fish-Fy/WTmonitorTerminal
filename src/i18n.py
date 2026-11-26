"""Display language and message formatting for the desktop and phone mirror."""

from .locale_en import MESSAGES
from .locale_maps import MAP_NAMES

DEFAULT_LANGUAGE = "zh-CN"
LANGUAGES = (("zh-CN", "简体中文"), ("en", "English"))
_language = DEFAULT_LANGUAGE


def normalize_language(value: str) -> str:
    return value if value in ("zh-CN", "en") else DEFAULT_LANGUAGE


def set_language(value: str) -> None:
    global _language
    _language = normalize_language(value)


def language() -> str:
    return _language


def tr(message: str, *values, locale: str | None = None) -> str:
    if (locale or _language) == "en":
        message = MESSAGES.get(message, message)
    return message.format(*values) if values else message


def map_name(scene: str) -> str:
    return MAP_NAMES.get(scene, scene) if _language == "en" else scene


def matches_map(scene: str, query: str) -> bool:
    return query in scene.casefold() or query in MAP_NAMES.get(scene, "").casefold()
