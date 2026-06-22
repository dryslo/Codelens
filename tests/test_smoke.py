from fastapi import FastAPI

from src.indexing.parsers.base import get_parser


def test_route_modules_generate_openapi():
    # openapi() принудительно резолвит аннотации сигнатур: ловит форвард-рефы
    # (future-annotations + типы под TYPE_CHECKING), которые ломают FastAPI-роуты.
    import services.inference_app as inference
    import services.llm_app as llm
    from src.admin.router import router as admin_router
    from src.auth.router import router as auth_router

    assert inference.app.openapi()["paths"]
    assert llm.app.openapi()["paths"]
    for r in (admin_router, auth_router):
        app = FastAPI()
        app.include_router(r)
        assert app.openapi()["paths"]


def test_chunk_id_format():
    code = ("class Repo:\n"
            "    def create(self):\n"
            "        return 1\n\n"
            "def create():\n"
            "    return 2\n")
    chunks = get_parser(".py").parse("gymhero/crud/user.py", code, "codebase")
    ids = {c.chunk_id for c in chunks}
    # метод -> ClassName.method, функция -> bare name; формат path:name:line
    assert "gymhero/crud/user.py:Repo:1" in ids
    assert "gymhero/crud/user.py:Repo.create:2" in ids
    assert "gymhero/crud/user.py:create:5" in ids


def test_unknown_extension():
    assert get_parser(".xyz") is None
