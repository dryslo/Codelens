"""мультиязычные парсеры (tree-sitter). Пропускаются, если extra `parsers` не установлен."""
import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_javascript")

from src.indexing.parsers.base import get_parser  # noqa: E402


def _parse(ext: str, code: str, fname: str):
    p = get_parser(ext)
    assert p is not None, f"нет парсера для {ext}"
    chunks = p.parse(fname, code, "demo")
    by_name = {c.name: c for c in chunks}            # для имён без коллизий
    triples = {(c.name, c.type, c.parent) for c in chunks}
    return chunks, by_name, triples


def test_javascript():
    code = """
function getUser(id) { return db.find(id); }
class UserRepo { findById(x) { return fetchRow(x); } }
const buildURL = (p) => normalize(p);
"""
    chunks, by, _t = _parse(".js", code, "src/user.js")
    assert by["getUser"].type == "function"
    assert by["UserRepo"].type == "class"
    assert by["findById"].type == "method" and by["findById"].parent == "UserRepo"
    assert by["buildURL"].type == "function"            # arrow-const
    assert by["getUser"].lang == "javascript"
    assert "find" in by["getUser"].calls
    assert by["findById"].chunk_id == "src/user.js:UserRepo.findById:3"


def test_typescript_and_tsx():
    code = "interface Store { put(): void; }\nclass Impl { save(x: number) { return persist(x); } }\n"
    for ext, lang in [(".ts", "typescript"), (".tsx", "tsx")]:
        _, by, _t = _parse(ext, code, f"a{ext}")
        assert by["Store"].type == "class"               # interface как контейнер
        assert by["Impl"].type == "class"
        assert by["save"].type == "method" and by["save"].parent == "Impl"
        assert by["save"].lang == lang


def test_go():
    code = """
package main
func GetUser(id int) string { return fetchRow(id) }
type Repo struct{ x int }
func (r *Repo) FindById(x int) int { return helper(x) }
"""
    _, by, _t = _parse(".go", code, "repo.go")
    assert by["GetUser"].type == "function"
    assert by["Repo"].type == "class"
    assert by["FindById"].type == "method" and by["FindById"].parent == "Repo"
    assert "helper" in by["FindById"].calls


def test_java():
    code = """
class UserRepo {
  UserRepo() {}
  public User findById(int id) { return fetchRow(id); }
}
interface Store { void put(); }
"""
    _, by, triples = _parse(".java", code, "UserRepo.java")
    # class и constructor оба зовутся UserRepo - сверка по тройкам (name, type, parent)
    assert ("UserRepo", "class", None) in triples
    assert ("UserRepo", "method", "UserRepo") in triples       # конструктор
    assert ("findById", "method", "UserRepo") in triples
    assert ("Store", "class", None) in triples
    assert "fetchRow" in by["findById"].calls


def test_bash():
    code = "function deploy_all() { build; }\nrun_tests() { echo hi; }\n"
    _, by, _t = _parse(".sh", code, "ops.sh")
    assert by["deploy_all"].type == "function"
    assert by["run_tests"].type == "function"


def test_c():
    code = "int get_user(int id) { return fetch_row(id); }\nstruct Repo { int x; };\n"
    _, by, _t = _parse(".c", code, "user.c")
    assert by["get_user"].type == "function"
    assert by["Repo"].type == "class"
    assert "fetch_row" in by["get_user"].calls


def test_cpp():
    code = ("class UserRepo { public: int findById(int x){ return fetchRow(x); } };\n"
            "int getUser(int id){ return 1; }\n")
    _, by, _t = _parse(".cpp", code, "user.cpp")
    assert by["UserRepo"].type == "class"
    assert by["findById"].type == "method" and by["findById"].parent == "UserRepo"
    assert by["getUser"].type == "function"


def test_rust():
    code = """
fn get_user(id: u32) -> String { fetch_row(id) }
struct Repo { x: i32 }
impl Repo { fn find_by_id(&self, x: i32) -> i32 { helper(x) } }
trait Store { fn put(&self); }
"""
    _, by, triples = _parse(".rs", code, "repo.rs")
    assert by["get_user"].type == "function"
    assert ("Repo", "class", None) in triples                 # struct
    assert ("find_by_id", "method", "Repo") in triples        # метод из impl
    assert ("Store", "class", None) in triples                # trait
    assert ("put", "method", "Store") in triples              # сигнатура метода в trait
    assert "helper" in by["find_by_id"].calls


def test_csharp():
    code = ("namespace App {\n"
            "  class UserRepo { public int FindById(int id){ return FetchRow(id); } UserRepo(){} }\n"
            "  interface IStore { void Put(); }\n"
            "}\n")
    _, by, triples = _parse(".cs", code, "Repo.cs")
    assert ("UserRepo", "class", None) in triples
    assert ("FindById", "method", "UserRepo") in triples
    assert ("UserRepo", "method", "UserRepo") in triples       # конструктор
    assert ("IStore", "class", None) in triples
    assert "FetchRow" in by["FindById"].calls


def test_ruby():
    code = ("class UserRepo\n  def find_by_id(x)\n    fetch_row(x)\n  end\nend\n"
            "module Store\n  def put; end\nend\n")
    _, by, triples = _parse(".rb", code, "repo.rb")
    assert ("UserRepo", "class", None) in triples
    assert ("find_by_id", "method", "UserRepo") in triples
    assert ("Store", "class", None) in triples                 # module
    assert ("put", "method", "Store") in triples


def test_php():
    code = ("<?php\n"
            "function getUser($id){ return fetchRow($id); }\n"
            "class UserRepo { public function findById($x){ return fetchRow($x); } }\n"
            "interface Store { public function put(); }\n")
    _, by, triples = _parse(".php", code, "repo.php")
    assert ("getUser", "function", None) in triples
    assert ("UserRepo", "class", None) in triples
    assert ("findById", "method", "UserRepo") in triples
    assert ("Store", "class", None) in triples


def test_docstrings_extracted():
    # block-комментарии (JSDoc/Javadoc/PHPDoc) и line-комментарии (GoDoc//, Rust///, Ruby#)
    cases = [
        (".js", "/** Finds a user. */\nfunction getUser(id){ return 1; }\n", "getUser", "Finds a user."),
        (".java", "/** Repo of users. */\nclass UserRepo { void f(){} }\n", "UserRepo", "Repo of users."),
        (".go", "// GetUser returns a user.\n// Second line.\nfunc GetUser() int { return 1 }\n",
         "GetUser", "GetUser returns a user.\nSecond line."),
        (".rs", "/// Finds by id.\nfn find_by_id() -> i32 { 1 }\n", "find_by_id", "Finds by id."),
        (".rb", "# Finds a user.\ndef find_user; end\n", "find_user", "Finds a user."),
        (".php", "<?php\n/** Gets user. */\nfunction getUser(){ return 1; }\n", "getUser", "Gets user."),
    ]
    for ext, code, name, expected in cases:
        _, by, _t = _parse(ext, code, f"a{ext}")
        assert by[name].docstring == expected, (ext, by[name].docstring)


def test_no_docstring_when_absent():
    _, by, _t = _parse(".js", "function bare(){ return 1; }\n", "a.js")
    assert by["bare"].docstring is None


def test_python_still_default():
    # регрессия: Python по-прежнему через ast, не tree-sitter
    _, by, _t = _parse(".py", "def foo():\n    return bar()\n", "m.py")
    assert by["foo"].type == "function" and by["foo"].lang == "python"
