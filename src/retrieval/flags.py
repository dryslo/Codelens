"""Переключатели поискового пайплайна - комбинируются произвольно на каждый запрос.

bm25       - лексический канал, сливается с dense через RRF
multiquery - LLM генерит N переформулировок, каждая идёт отдельной dense-выдачей в RRF
hyde       - LLM генерит гипотетический фрагмент кода, добавляемый к запросу
rerank     - топ-N кандидатов через кросс-энкодер
mmr        - диверсификация финальной выдачи (после rerank/фьюжна)

FlagsPolicy - деплойная конфигурация: для каждого канала один из режимов off/ui/thinking/fast.
Применяется на каждом search() и переопределяет присланное клиентом.
"""
from dataclasses import asdict, dataclass
from typing import Any

FLAG_NAMES = ("bm25", "multiquery", "hyde", "rerank", "mmr")
POLICY_MODES = ("off", "ui", "thinking", "fast")


@dataclass
class SearchFlags:
    """Набор переключателей каналов поиска и их параметров на один запрос."""

    # Добавочные каналы выключены по умолчанию: на корпусе small они нейтральны либо
    # ухудшают precision (docs/retrieval-eval.md). Включаются через UI или политикой
    # в config.yaml (retrieval.flags).
    bm25: bool = False
    multiquery: bool = False
    hyde: bool = False
    rerank: bool = False
    mmr: bool = False
    k_cand: int = 50
    mmr_lambda: float = 0.7
    multiquery_n: int = 3

    @classmethod
    def from_mode(cls, mode: str | None) -> "SearchFlags":
        """Стартовый набор без учёта политики (используется когда политики нет)."""
        if mode == "thinking":
            return cls(bm25=True, multiquery=False, hyde=False, rerank=False, mmr=False)
        return cls()  # fast: только dense

    @classmethod
    def from_any(cls, obj: Any) -> "SearchFlags":
        """Привести None/SearchFlags/dict к SearchFlags."""
        if obj is None:
            return cls()
        if isinstance(obj, cls):
            return obj
        if isinstance(obj, dict):
            valid = {k: v for k, v in obj.items() if k in cls.__dataclass_fields__}
            return cls(**valid)
        raise TypeError(f"cannot build SearchFlags from {type(obj)}")

    def to_dict(self) -> dict:
        """Сериализовать флаги в dict."""
        return asdict(self)


@dataclass
class FlagsPolicy:
    """Деплойная политика по каждому каналу. Значения - одно из POLICY_MODES.

    off      - выключен принудительно: не виден в UI и игнорируется, даже если клиент прислал True.
    ui       - решает пользователь в UI (или через флаги CLI/REST).
    thinking - авто-включается в режиме "thinking"; в "fast" выкл.
    fast     - авто-включается в обоих режимах (входит в fast-пресет, а значит и в thinking).

    Дефолты под docs/retrieval-eval.md (small-корпус): все каналы в UI и выкл;
    rerank - off (без cross-encoder).
    """

    bm25: str = "ui"
    multiquery: str = "ui"
    hyde: str = "ui"
    rerank: str = "off"
    mmr: str = "ui"

    @classmethod
    def from_config(cls, cfg: dict | None) -> "FlagsPolicy":
        """Построить политику из секции config.yaml (только валидные режимы)."""
        if not cfg:
            return cls()
        kw = {}
        for name in FLAG_NAMES:
            v = cfg.get(name)
            if isinstance(v, str) and v in POLICY_MODES:
                kw[name] = v
        return cls(**kw)

    def items(self) -> list[tuple[str, str]]:
        """Пары (имя канала, режим) по всем каналам."""
        return [(n, getattr(self, n)) for n in FLAG_NAMES]

    def ui_visible(self) -> list[str]:
        """Каналы в режиме ui (видимы и управляемы из UI)."""
        return [n for n, m in self.items() if m == "ui"]

    def forced_for(self, mode: str | None) -> dict[str, bool]:
        """Какие каналы политика принудительно включает/выключает в данном mode.

        True  - принудительно on, False - принудительно off, отсутствие ключа - оставить как есть.
        """
        out: dict[str, bool] = {}
        for name, m in self.items():
            if m == "off":
                out[name] = False
            elif m == "fast":
                out[name] = True
            elif m == "thinking":
                out[name] = (mode == "thinking")
        return out

    def apply(self, flags: SearchFlags, mode: str | None = None) -> SearchFlags:
        """Переписать поля SearchFlags по политике (off/fast/thinking). ui - не трогаем."""
        for name, val in self.forced_for(mode).items():
            setattr(flags, name, val)
        return flags

    def defaults(self, mode: str | None = "fast") -> SearchFlags:
        """Стартовое состояние UI: ui-каналы выкл, остальные - по политике/режиму."""
        f = SearchFlags()
        for name, val in self.forced_for(mode).items():
            setattr(f, name, val)
        return f

    def to_dict(self) -> dict:
        """Сериализовать политику в dict {канал: режим}."""
        return {n: getattr(self, n) for n in FLAG_NAMES}
