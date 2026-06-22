# indexing/parsers - фабрика парсеров и Python-парсер

Два файла: `python_ast.py` (парсер Python через `ast`) и `base.py` (реестр по расширению).

## python_ast.py

### `_calls(node)` - кого вызывает фрагмент
```python
def _calls(node) -> list[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return sorted(out)
```
- `ast.walk(node)` рекурсивно обходит всё поддерево узла (тело функции/класса).
- `ast.Call` - узел вызова. Его `.func` бывает двух видов:
  - `ast.Name` (`foo()`) → имя в `f.id`;
  - `ast.Attribute` (`obj.method()`) → имя метода в `f.attr` (объект игнорируется - важно имя вызываемого).
- `set` убирает дубли, `sorted` даёт стабильный порядок (для воспроизводимости `enriched_text`). Эти имена идут в `Chunk.calls` и в обогащённый текст («Вызывает: …») - недорогой сигнал связей графа вызовов.

### Класс `PythonAstParser`
```python
class PythonAstParser(Parser):
    extensions = {".py"}
    lang = "python"
```
- Регистрируется в реестре по `.py`. `lang` попадёт в `Chunk.lang` (для подсветки).

```python
    def parse(self, path, source, source_name) -> list[Chunk]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
```
- `ast.parse` строит AST из текста файла. Битый файл (`SyntaxError`) не должен ронять всю индексацию, поэтому возвращается пустой список (файл пропускается).

```python
        rel = Path(path).as_posix()
```
- `as_posix()` - прямые слэши `/` даже на Windows. Важно для scorer: эталонные `chunk_id` используют `gymhero/security.py` с `/`.

```python
        def make(node, ctype, parent=None) -> Chunk:
            qualified = f"{parent}.{node.name}" if parent else node.name
            return Chunk(
                chunk_id=f"{rel}:{qualified}:{node.lineno}",   # формат scorer
                ...
                start_line=node.lineno, end_line=node.end_lineno or node.lineno,
                code=ast.get_source_segment(source, node) or "",
                docstring=ast.get_docstring(node), calls=_calls(node),
            )
```
- `qualified`: для метода это `ClassName.method` (есть `parent`), для функции/класса - голое имя. Официальный `score.py` различает функцию `create` и метод `Repo.create` - у них разные эталонные id.
- `chunk_id=f"{rel}:{qualified}:{node.lineno}"` - строго формат scorer `{path}:{name}:{line}`.
- `node.lineno` - строка `def`/`class` (scorer сверяет с допуском ±2).
- `end_lineno or node.lineno` - на очень старых грамматиках `end_lineno` может быть `None`; fallback на начало.
- `ast.get_source_segment(source, node)` - вырезает исходный текст фрагмента (с отступами как в файле); `or ""` - страховка от `None`.
- `ast.get_docstring(node)` - докстринг функции/класса/метода (или `None`).

```python
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                chunks.append(make(node, "function"))
            elif isinstance(node, ast.ClassDef):
                chunks.append(make(node, "class"))
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        chunks.append(make(sub, "method", parent=node.name))
        return chunks
```
- Обход по верхнему уровню модуля (`tree.body`):
  - функции (обычные и `async`) → чанк `function`;
  - класс → чанк `class`, плюс проход по телу класса и каждый метод (обычный/async) → чанк `method` с `parent=имя класса`.
- Гранулярность: функция/метод/класс - естественная семантическая единица; файл целиком слишком грубо, строки - слишком мелко. Вложенные функции внутри функций намеренно не выделяются (редки, дробят индекс) - при необходимости добавляются рекурсией.

## base.py - реестр (фабрика по расширению)
```python
_PARSERS: dict = {}
def register(parser):
    for ext in parser.extensions:
        _PARSERS[ext] = parser
def get_parser(ext: str):
    return _PARSERS.get(ext)
register(PythonAstParser())
register_treesitter(register)
```
- `_PARSERS` - словарь `расширение → экземпляр парсера`. `register` раскладывает один парсер по всем его расширениям. `get_parser` возвращает парсер или `None` (тогда файл пропускается в пайплайне).
- Внизу регистрируется Python-парсер (через `ast`), затем `register_treesitter` добавляет мультиязычные парсеры на tree-sitter (см. `treesitter.py`). Добавление языка = класс с `extensions`/`lang`/`parse` плюс `register(...)` - пайплайн/эмбеддер/поиск/UI не затрагиваются, так как все работают с общим `Chunk`.
- `registered_langs()` - список языков с зарегистрированным парсером (фолбэк для UI-фильтра, когда стор не перечисляет источники).

Реестр - модульный синглтон, заполняется на импорте `base.py`. Парсер в отдельном модуле нужно импортировать так, чтобы `register` выполнился.
