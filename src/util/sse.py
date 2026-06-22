"""SSE-обёртка для стриминга токенов LLM.

Одна дельта = одна data:-строка, тело - JSON {"t": ...}: переживает перевод строки в токенах
(в SSE сырой \\n завершил бы поле). Сентинел конца - [DONE]. Сервер использует pack/done,
клиент - parse_lines.
"""  # noqa: D301
import json
from collections.abc import Iterable, Iterator

DONE = "[DONE]"


def pack(delta: str) -> str:
    """Упаковать дельту токена в SSE-строку data: с JSON-телом."""
    return f"data: {json.dumps({'t': delta}, ensure_ascii=False)}\n\n"


def done() -> str:
    """Вернуть SSE-строку-сентинел конца потока."""
    return f"data: {DONE}\n\n"


def stream(deltas: Iterable[str]) -> Iterator[str]:
    """Обернуть поток дельт в SSE: pack каждой + завершающий done().

    done() отдаётся и при ошибке источника - иначе клиент висит до таймаута без сентинела.
    Обрыв клиента (GeneratorExit) - BaseException, не глушится: поток просто закрывается.
    """
    try:
        for d in deltas:
            yield pack(d)
    except Exception:  # noqa: BLE001 - ошибка источника не должна оставить клиента без [DONE]
        pass
    yield done()


def parse_lines(lines: Iterator[str]) -> Iterator[str]:
    """Из итератора SSE-строк (decode_unicode=True) достаёт дельты до [DONE]."""
    for line in lines:
        if not line or not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == DONE:
            break
        yield json.loads(payload)["t"]
