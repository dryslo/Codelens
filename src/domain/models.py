"""Доменные модели данных."""
from dataclasses import dataclass, field


@dataclass
class Chunk:
    """Единица индексации: function | class | method с метаданными."""

    chunk_id: str
    source: str            # имя репозитория/набора (для админки)
    lang: str              # "python" | "javascript" | ...
    file: str
    type: str              # function | class | method
    name: str
    parent: str | None     # имя класса для метода, иначе None
    start_line: int
    end_line: int
    code: str
    docstring: str | None
    calls: list[str] = field(default_factory=list)

    def enriched_text(self) -> str:
        """Текст для эмбеддинга: NL, а не сырой код (RU-запрос - EN-код)."""
        head = (f"Метод {self.name} класса {self.parent}." if self.parent
                else f"{'Класс' if self.type == 'class' else 'Функция'} {self.name}.")
        parts = [head]
        if self.docstring:
            parts.append(self.docstring)
        parts.append(self.code.splitlines()[0] if self.code else "")
        if self.calls:
            parts.append("Вызывает: " + ", ".join(self.calls[:10]))
        return "\n".join(parts)
