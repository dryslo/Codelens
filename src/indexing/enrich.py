"""Обогащение чанка перед эмбеддингом.

Базовый NL-текст даёт `Chunk.enriched_text()`. Дополнительно подмешиваются разбитые на
слова идентификаторы (`getUserById` → `get user by id`), чтобы запросы на естественном
языке (в т.ч. русские) лучше матчились на camelCase/snake_case-имена. Оригинальные имена
остаются в `enriched_text()` (нужны для точного совпадения / bm25).
"""
import re

from src.domain.models import Chunk

# Границы для разбиения идентификатора на слова (применяются по очереди).
_RE_SEP = re.compile(r"[_\-.]+")              # snake_case / kebab-case / dotted
_RE_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")  # HTTPClient → HTTP Client
_RE_CAMEL = re.compile(r"([a-z\d])([A-Z])")        # fooBar → foo Bar
_RE_LETTER_DIGIT = re.compile(r"([A-Za-z])(\d)")   # parse2 → parse 2
_RE_DIGIT_LETTER = re.compile(r"(\d)([A-Za-z])")   # 2nd → 2 nd


def humanize_identifier(name: str) -> str:
    """Разбивает идентификатор на слова в нижнем регистре (camelCase / snake_case / акронимы)."""
    s = _RE_SEP.sub(" ", name)
    s = _RE_ACRONYM.sub(r"\1 \2", s)
    s = _RE_CAMEL.sub(r"\1 \2", s)
    s = _RE_LETTER_DIGIT.sub(r"\1 \2", s)
    s = _RE_DIGIT_LETTER.sub(r"\1 \2", s)
    return " ".join(s.split()).lower()


def _humanized_tokens(chunk: Chunk) -> list[str]:
    """Разбитые имена символа/класса/вызовов - только когда разбиение реально что-то дало."""
    raw = [chunk.name, *([chunk.parent] if chunk.parent else []), *chunk.calls[:10]]
    out: list[str] = []
    for n in raw:
        h = humanize_identifier(n)
        if h and h != (n or "").lower() and h not in out:
            out.append(h)
    return out


def _path_tokens(file: str) -> str:
    """`auth/user_repo.py` → `auth user repo` (без расширения)."""
    stem = re.sub(r"\.[^./]+$", "", file)
    return humanize_identifier(stem.replace("/", " "))


def enrich(chunk: Chunk) -> str:
    """NL-текст чанка плюс разбитые идентификаторы и токены пути - для эмбеддинга."""
    parts = [chunk.enriched_text()]
    toks = _humanized_tokens(chunk)
    if toks:
        parts.append("Идентификаторы: " + ", ".join(toks))
    path = _path_tokens(chunk.file)
    if path:
        parts.append("Путь: " + path)
    return "\n".join(parts)
