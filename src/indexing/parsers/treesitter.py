"""Мультиязычные парсеры на tree-sitter (JS/TS/TSX, Go, Java, Bash, C, C++).

Один конфиг-движок: на язык задаём типы узлов (функции / контейнеры / методы), имя
символа достаём через field `name` с языковыми фолбэками (C/C++ - через `declarator`,
Go - `type_spec`/receiver, JS - arrow-`const`). Формат `Chunk` и `chunk_id` - как у
Python-парсера (`{rel}:{qualified}:{start_line}`), поэтому эмбеддер/поиск/UI не меняются.

Грамматики вшиты в wheel-ы (extra `parsers`); регистрируются опционально из parsers/base.py.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_bash
import tree_sitter_c
import tree_sitter_c_sharp
import tree_sitter_cpp
import tree_sitter_go
import tree_sitter_java
import tree_sitter_javascript
import tree_sitter_php
import tree_sitter_ruby
import tree_sitter_rust
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from src.domain.interfaces import Parser as ParserPort
from src.domain.models import Chunk

# "name" - лист-идентификатор в PHP (в др. грамматиках это поле, не тип узла, → безопасно).
_IDENT = {"identifier", "field_identifier", "type_identifier", "property_identifier", "name"}
_BODY_TYPES = {"class_body", "field_declaration_list", "interface_body",
               "enum_body", "declaration_list", "body_statement"}
# Узлы-комментарии (JSDoc/Javadoc/PHPDoc - block; GoDoc/// /# - line) для извлечения docstring.
_COMMENT = {"comment", "line_comment", "block_comment"}
_LINE_MARK = re.compile(r"^\s*(///?|#+|--)\s?")   # //, ///, #, -- в начале строки
_STAR_MARK = re.compile(r"^\s*\*+\s?")            # ведущая * в блочном комментарии


def _clean_comment(text: str) -> str:
    text = text.strip()
    if text.startswith("/*"):
        text = text[2:]
        if text.endswith("*/"):
            text = text[:-2]
        lines = [_STAR_MARK.sub("", ln) for ln in text.splitlines()]
    else:
        lines = [_LINE_MARK.sub("", ln) for ln in text.splitlines()]
    return "\n".join(lines).strip()


def _docstring(node: Node, src: bytes) -> str | None:
    """Док-комментарий: смежные узлы-комментарии непосредственно перед определением."""
    parts: list[str] = []
    sib = node.prev_named_sibling
    while sib is not None and sib.type in _COMMENT:
        parts.append(_clean_comment(_text(sib, src)))
        sib = sib.prev_named_sibling
    if not parts:
        return None
    parts.reverse()
    return "\n".join(p for p in parts if p).strip() or None
_FUNC_VALUE = {"arrow_function", "function", "function_expression", "generator_function"}


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _body_of(node: Node) -> Node:
    """Узел тела контейнера/функции (с языковыми фолбэками)."""
    b = node.child_by_field_name("body")
    if b is not None:
        return b
    for c in node.named_children:
        if c.type in _BODY_TYPES:
            return c
    return node  # fallback: методы - прямые дети контейнера (Ruby class/module)


def _type_name(node: Node | None, src: bytes) -> str | None:
    """Имя типа из узла-типа (Rust `impl Repo` / `impl Trait for Repo` → Repo)."""
    if node is None:
        return None
    if node.type == "type_identifier":
        return _text(node, src)
    for d in _walk(node):
        if d.type == "type_identifier":
            return _text(d, src)
    return _text(node, src)


def _name_of(node: Node, src: bytes) -> str | None:
    """Имя символа с языковыми фолбэками."""
    # Go: type Foo struct{...} - имя в type_spec.
    if node.type == "type_declaration":
        for c in node.named_children:
            if c.type == "type_spec":
                n = c.child_by_field_name("name")
                return _text(n, src) if n else None
        return None
    # JS/TS: const foo = (..) => ..  /  const bar = function(){}.
    if node.type in ("lexical_declaration", "variable_declaration"):
        for c in node.named_children:
            if c.type == "variable_declarator":
                val = c.child_by_field_name("value")
                if val is not None and val.type in _FUNC_VALUE:
                    n = c.child_by_field_name("name")
                    return _text(n, src) if n else None
        return None
    # Общий случай (js/go/java/bash function_definition, классы, методы).
    n = node.child_by_field_name("name")
    if n is not None:
        return _text(n, src)
    # C/C++: имя зарыто в declarator → ... → function_declarator → identifier.
    d = node.child_by_field_name("declarator")
    while d is not None:
        if d.type in _IDENT:
            return _text(d, src)
        nxt = d.child_by_field_name("declarator")
        if nxt is None:
            for c in d.named_children:
                if c.type in _IDENT or c.type in ("qualified_identifier", "destructor_name",
                                                  "operator_name"):
                    return _text(c, src)
            return None
        d = nxt
    return None


def _cpp_qualified_split(node: Node, src: bytes) -> tuple[str | None, str | None]:
    """Внеклассовое C++-определение `RetType Scope::name(){...}` → (Scope, name).

    Имя зарыто в declarator → function_declarator → qualified_identifier. Спускаемся по
    полю `name` (вложенные `ns::Outer::method`) до листа; ближайший `scope` - класс/namespace.
    Возвращает (None, None), если квалифицированного имени нет (обычная top-level функция).
    """
    d = node.child_by_field_name("declarator")
    while d is not None and d.type != "qualified_identifier":
        d = d.child_by_field_name("declarator")
    if d is None:
        return None, None
    while True:
        name = d.child_by_field_name("name")
        if name is not None and name.type == "qualified_identifier":
            d = name
            continue
        scope = d.child_by_field_name("scope")
        scope_text = _text(scope, src) if scope is not None else None
        name_text = _text(name, src) if name is not None else None
        return scope_text, name_text


def _go_receiver(node: Node, src: bytes) -> str | None:
    r = node.child_by_field_name("receiver")  # (r *Repo) → ищем type_identifier на любой глубине
    if r is None:
        return None
    for d in _walk(r):
        if d.type == "type_identifier":
            return _text(d, src)
    return None


def _calls(node: Node, src: bytes, call_types: set) -> list[str]:
    if not call_types:
        return []
    out: set[str] = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in call_types:
            fn = n.child_by_field_name("function") or n.child_by_field_name("name") or n
            toks = [d for d in _walk(fn) if d.type in _IDENT]
            if toks:  # самый правый идентификатор = имя вызываемого (db.find → find)
                out.add(_text(max(toks, key=lambda d: d.start_byte), src))
        stack.extend(n.children)
    return sorted(out)[:10]


def _walk(node: Node) -> Iterator[Node]:
    """Обход поддерева в глубину (включая сам узел)."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


@dataclass
class LangSpec:
    """Конфиг языка: типы узлов функций/контейнеров/методов/вызовов для tree-sitter."""

    name: str
    extensions: set
    language: object
    func_types: set = field(default_factory=set)    # standalone-функции
    class_types: set = field(default_factory=set)    # контейнеры (+ методы из тела)
    method_types: set = field(default_factory=set)   # методы внутри контейнера
    call_types: set = field(default_factory=set)
    impl_types: set = field(default_factory=set)     # безымянные контейнеры методов (Rust impl)
    impl_parent_field: str = "type"                  # поле узла impl → имя типа (parent методов)


_TS = tree_sitter_typescript
SPECS = [
    LangSpec("javascript", {".js", ".jsx", ".mjs", ".cjs"}, tree_sitter_javascript.language(),
             func_types={"function_declaration", "generator_function_declaration",
                         "lexical_declaration", "variable_declaration"},
             class_types={"class_declaration"}, method_types={"method_definition"},
             call_types={"call_expression"}),
    LangSpec("typescript", {".ts", ".mts", ".cts"}, _TS.language_typescript(),
             func_types={"function_declaration", "lexical_declaration", "variable_declaration"},
             class_types={"class_declaration", "abstract_class_declaration", "interface_declaration"},
             method_types={"method_definition"}, call_types={"call_expression"}),
    LangSpec("tsx", {".tsx"}, _TS.language_tsx(),
             func_types={"function_declaration", "lexical_declaration", "variable_declaration"},
             class_types={"class_declaration", "abstract_class_declaration", "interface_declaration"},
             method_types={"method_definition"}, call_types={"call_expression"}),
    LangSpec("go", {".go"}, tree_sitter_go.language(),
             func_types={"function_declaration", "method_declaration"},
             class_types={"type_declaration"}, call_types={"call_expression"}),
    LangSpec("java", {".java"}, tree_sitter_java.language(),
             class_types={"class_declaration", "interface_declaration", "enum_declaration"},
             method_types={"method_declaration", "constructor_declaration"},
             call_types={"method_invocation"}),
    LangSpec("bash", {".sh", ".bash"}, tree_sitter_bash.language(),
             func_types={"function_definition"}),
    LangSpec("c", {".c", ".h"}, tree_sitter_c.language(),
             func_types={"function_definition"}, class_types={"struct_specifier"},
             call_types={"call_expression"}),
    LangSpec("cpp", {".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"}, tree_sitter_cpp.language(),
             func_types={"function_definition"},
             class_types={"class_specifier", "struct_specifier"},
             method_types={"function_definition"}, call_types={"call_expression"}),
    LangSpec("rust", {".rs"}, tree_sitter_rust.language(),
             func_types={"function_item"},
             class_types={"struct_item", "enum_item", "trait_item", "union_item"},
             method_types={"function_item", "function_signature_item"},
             impl_types={"impl_item"}, impl_parent_field="type",
             call_types={"call_expression"}),
    LangSpec("csharp", {".cs"}, tree_sitter_c_sharp.language(),
             class_types={"class_declaration", "interface_declaration", "struct_declaration",
                          "record_declaration", "enum_declaration"},
             method_types={"method_declaration", "constructor_declaration"},
             call_types={"invocation_expression"}),
    LangSpec("ruby", {".rb"}, tree_sitter_ruby.language(),
             func_types={"method", "singleton_method"},
             class_types={"class", "module"},
             method_types={"method", "singleton_method"}, call_types={"call"}),
    LangSpec("php", {".php"}, tree_sitter_php.language_php(),
             func_types={"function_definition"},
             class_types={"class_declaration", "interface_declaration", "trait_declaration",
                          "enum_declaration"},
             method_types={"method_declaration"},
             call_types={"function_call_expression", "member_call_expression",
                         "scoped_call_expression"}),
]


class TreeSitterParser(ParserPort):
    """Парсер одного языка на tree-sitter по конфигу LangSpec."""

    def __init__(self, spec: LangSpec) -> None:
        self.spec = spec
        self.extensions = spec.extensions
        self.lang = spec.name
        self._parser = Parser(Language(spec.language))

    def parse(self, path: str, source: str, source_name: str) -> list[Chunk]:
        """Разобрать исходник в список Chunk (функции, классы, методы)."""
        src = source.encode("utf-8")
        root = self._parser.parse(src).root_node
        rel = Path(path).as_posix()
        spec = self.spec
        chunks: list[Chunk] = []

        def make(node: Node, ctype: str, name: str, parent: str | None) -> Chunk:
            qualified = f"{parent}.{name}" if parent else name
            start = node.start_point[0] + 1
            return Chunk(
                chunk_id=f"{rel}:{qualified}:{start}",
                source=source_name, lang=spec.name, file=rel, type=ctype,
                name=name, parent=parent,
                start_line=start, end_line=node.end_point[0] + 1,
                code=_text(node, src), docstring=_docstring(node, src),
                calls=_calls(node, src, spec.call_types),
            )

        def emit(node: Node, parent: str | None) -> None:
            name = _name_of(node, src)
            if not name:
                return
            ctype = "class" if node.type in spec.class_types else ("method" if parent else "function")
            chunks.append(make(node, ctype, name, parent))

        def visit(node: Node) -> None:
            t = node.type
            if t in spec.impl_types:  # Rust impl: безымянный контейнер, parent = тип из поля
                parent = _type_name(node.child_by_field_name(spec.impl_parent_field), src)
                body = _body_of(node)
                for ch in body.named_children:
                    if ch.type in spec.method_types:
                        emit(ch, parent)
                return
            if t in spec.class_types:
                cname = _name_of(node, src)
                if cname:
                    chunks.append(make(node, "class", cname, None))
                    body = _body_of(node)
                    if body is not None and spec.method_types:
                        for ch in body.named_children:
                            if ch.type in spec.method_types:
                                emit(ch, parent=cname)
                return  # не углубляемся в тело контейнера
            if t in spec.func_types:
                if t == "method_declaration":
                    emit(node, _go_receiver(node, src))
                    return  # не углубляемся в тело функции
                # C++: внеклассовое определение `Scope::name(){...}` - метод класса Scope.
                if spec.name == "cpp":
                    scope, qname = _cpp_qualified_split(node, src)
                    if scope and qname:
                        chunks.append(make(node, "method", qname, scope))
                        return  # не углубляемся в тело функции
                emit(node, None)
                return  # не углубляемся в тело функции
            for ch in node.named_children:
                visit(ch)

        visit(root)
        return chunks


def register_treesitter(register: Callable[[ParserPort], None]) -> None:
    """Зарегистрировать парсеры всех языков из SPECS через переданный register."""
    for spec in SPECS:
        register(TreeSitterParser(spec))
