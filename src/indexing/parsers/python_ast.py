"""Парсер Python через стандартный ast: функции, классы и методы в Chunk."""
import ast
from pathlib import Path

from src.domain.interfaces import Parser
from src.domain.models import Chunk


def _calls(node: ast.AST) -> list[str]:
    """Отсортированные имена вызываемых функций/методов внутри узла."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return sorted(out)


class PythonAstParser(Parser):
    """chunk_id в формате scorer: {relative_path}:{name}:{start_line}.

    Для методов name = ClassName.method_name (функция `create` и метод `Repo.create` -
    разные chunk_id). relative_path - от корня репозитория, слэши '/'.
    """

    extensions = {".py"}
    lang = "python"

    def parse(self, path: str, source: str, source_name: str) -> list[Chunk]:
        """Разобрать исходник в список Chunk (функции, классы, методы)."""
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
        rel = Path(path).as_posix()
        chunks: list[Chunk] = []

        def make(node: ast.AST, ctype: str, parent: str | None = None) -> Chunk:
            qualified = f"{parent}.{node.name}" if parent else node.name
            return Chunk(
                chunk_id=f"{rel}:{qualified}:{node.lineno}",   # формат scorer
                source=source_name, lang=self.lang, file=rel, type=ctype,
                name=node.name, parent=parent,
                start_line=node.lineno, end_line=node.end_lineno or node.lineno,
                code=ast.get_source_segment(source, node) or "",
                docstring=ast.get_docstring(node), calls=_calls(node),
            )

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                chunks.append(make(node, "function"))
            elif isinstance(node, ast.ClassDef):
                chunks.append(make(node, "class"))
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        chunks.append(make(sub, "method", parent=node.name))
        return chunks
