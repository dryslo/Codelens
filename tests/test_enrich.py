"""нормализация идентификаторов в обогащённом тексте (enrich)."""
import pytest

from src.domain.models import Chunk
from src.indexing.enrich import enrich, humanize_identifier


@pytest.mark.parametrize("raw,expected", [
    ("getUserById", "get user by id"),
    ("user_repository", "user repository"),
    ("HTTPClient", "http client"),
    ("parseURLPath", "parse url path"),
    ("parseJSON2", "parse json 2"),
    ("user-repo", "user repo"),
    ("already_lower", "already lower"),
    ("ID", "id"),
    ("v2Migration", "v 2 migration"),
    ("snake_caseHTTP", "snake case http"),
])
def test_humanize_identifier(raw, expected):
    assert humanize_identifier(raw) == expected


def _chunk(**kw):
    base = dict(chunk_id="c1", source="s", lang="python", file="auth/user_repo.py",
                type="method", name="getUserById", parent="UserRepository",
                start_line=1, end_line=2, code="def getUserById(self): ...",
                docstring=None, calls=["fetchRow", "buildURL"])
    base.update(kw)
    return Chunk(**base)


def test_enrich_adds_humanized_identifiers_and_path():
    text = enrich(_chunk())
    # оригинальные имена сохранены для точного совпадения
    assert "getUserById" in text
    assert "UserRepository" in text
    # разбитые формы name/parent/calls
    assert "Идентификаторы:" in text
    assert "get user by id" in text
    assert "user repository" in text
    assert "fetch row" in text and "build url" in text
    # путь разбит в слова, без расширения
    assert "Путь: auth user repo" in text


def test_enrich_skips_noop_split():
    # имя без camel/snake не должно плодить дубль-строку идентификаторов
    text = enrich(_chunk(name="main", parent=None, calls=[]))
    assert "Идентификаторы:" not in text
    assert "Функция main." in text
