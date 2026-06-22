"""Реестр парсеров: фабрика по расширению файла.

Добавить язык = новый класс Parser (например на tree-sitter) + register(...).
Пайплайн, эмбеддер, поиск и UI не меняются - формат Chunk общий.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.indexing.parsers.python_ast import PythonAstParser
from src.indexing.parsers.treesitter import register_treesitter

if TYPE_CHECKING:
    from src.domain.interfaces import Parser

_PARSERS: dict = {}


def register(parser: Parser) -> None:
    """Зарегистрировать парсер для всех его расширений."""
    for ext in parser.extensions:
        _PARSERS[ext] = parser


def get_parser(ext: str) -> Parser | None:
    """Парсер по расширению файла (None, если язык не поддержан)."""
    return _PARSERS.get(ext)


def registered_langs() -> list[str]:
    """Языки с зарегистрированным парсером (фолбэк для UI-фильтра, когда стор не перечисляет)."""
    return sorted({lang for p in _PARSERS.values() if (lang := getattr(p, "lang", ""))})


register(PythonAstParser())
# Мультиязычные парсеры (tree-sitter) - по умолчанию. Грамматики в wheel-ах (extra `parsers`,
# входит в профили backend/worker/all). Python остаётся через ast.
register_treesitter(register)
