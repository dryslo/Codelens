# indexing - enrich.py и pipeline.py

## enrich.py - текст чанка для эмбеддинга
```python
def enrich(chunk: Chunk) -> str:
    parts = [chunk.enriched_text()]
    toks = _humanized_tokens(chunk)
    if toks:
        parts.append("Идентификаторы: " + ", ".join(toks))
    path = _path_tokens(chunk.file)
    if path:
        parts.append("Путь: " + path)
    return "\n".join(parts)
```
- Базовый NL-текст - `Chunk.enriched_text()`. Поверх подмешиваются разбитые на слова идентификаторы (`getUserById` → `get user by id`) и токены пути (`auth/user_repo.py` → `auth user repo`), чтобы запросы на естественном языке (в т.ч. русские) матчились на camelCase/snake_case-имена. Оригинальные имена остаются в `enriched_text()` - нужны для точного совпадения / bm25.
- `humanize_identifier` режет по границам snake_case / kebab-case / dotted, акронимам (`HTTPClient` → `HTTP Client`), camelCase и стыкам буква/цифра. `_humanized_tokens` берёт имя символа, родителя и вызовы, оставляя только токены, где разбиение реально что-то дало.
- Пайплайн вызывает `enrich(c)`, а не метод напрямую - единая точка, где меняется стратегия обогащения (например NL-описание чанка через LLM на индексации) без правок пайплайна.

## pipeline.py - индексация

### `_meta(c)` - что кладём в метаданные стора
```python
def _meta(c) -> dict:
    return {
        "chunk_id": c.chunk_id,    # формат scorer → попадёт в results.json
        "source": c.source, "lang": c.lang, "file": c.file, "type": c.type,
        "name": c.name, "parent": c.parent or "", "start_line": c.start_line,
        "end_line": c.end_line, "docstring": c.docstring or "",
        "calls": ",".join(c.calls),
    }
```
- Плоский dict (вектор-сторы хранят примитивы, не вложенные объекты). `chunk_id` обязателен в метаданных - именно его потом отдаём в результат и в `results.json`, а не внутренний ключ стора.
- `parent or ""`, `docstring or ""` - `None` нежелателен в payload некоторых сторов; заменяем на пустую строку.
- `calls` сворачиваем в строку через запятую (списки в метаданных Chroma не всегда поддержаны) - при показе можно разбить обратно.

### `_store_id(c)` - уникальный ключ в сторе
```python
def _store_id(c) -> str:
    return f"{c.source}::{c.chunk_id}"
```
- Отдельно от `chunk_id`: `chunk_id` (формат scorer) уникален в пределах одного источника, но при нескольких источниках два репо могут иметь одинаковый `path:name:line`. Чтобы исключить коллизии ключей в сторе, физический id = `source::chunk_id`. При этом в метаданных лежит чистый `chunk_id`, и в выдачу/`results.json` идёт он.

### `index_path(...)` - основной проход
```python
def index_path(folder, source, store, embedder, registry,
               incremental=True, batch=64, progress=None) -> dict:
    root = Path(folder)
    files = [p for p in root.rglob("*") if p.is_file() and get_parser(p.suffix)]
```
- `rglob("*")` - рекурсивный обход всех путей под `folder`. Фильтр: только файлы (`is_file()`) и только те, для которых есть парсер (`get_parser(p.suffix)` не `None`). Так индексируются все поддерживаемые языки (Python через `ast`, остальные через tree-sitter).
- `progress` - колбэк прогресса (см. `_report`), `batch` - размер батча эмбеддинга.

Проход двухфазный: сначала парсинг всех изменённых файлов в буферы, затем эмбеддинг батчами. Общее число чанков становится известно до тяжёлой части, и прогресс идёт по добавленным чанкам, а не по файлам.

```python
    current = {p.relative_to(root).as_posix() for p in files}
```
- Множество относительных путей, существующих на текущий момент - используется в конце, чтобы найти удалённые файлы.

Шаг 1 - парсинг изменённых файлов, сбор чанков в буферы:
```python
    for i, p in enumerate(files, 1):
        rel = p.relative_to(root).as_posix()
        text = p.read_text(encoding="utf-8", errors="ignore")
        h = hashlib.sha1(text.encode()).hexdigest()
        prev = registry.get_hash(source, rel)
        if incremental and prev == h:
            skipped += 1
            _report()
            continue
        store.delete_where(source=source, file=rel)
        chunks = get_parser(p.suffix).parse(rel, text, source)
        for c in chunks:
            texts.append(enrich(c)); ids.append(_store_id(c))
            metas.append(_meta(c)); codes.append(c.code)
        updated += 1 if prev else 0
        added += 0 if prev else 1
        registry.set_hash(source, rel, h)
```
- `rel` - путь относительно корня репо (даёт `gymhero/security.py` - формат scorer).
- `errors="ignore"` - не падать на не-UTF8 байтах.
- SHA1 содержимого + сравнение с сохранённым хэшем: если файл не менялся и режим инкрементальный - пропуск (быстрая переиндексация).
- Перед сбором старые чанки файла удаляются (`delete_where(source, file)`), иначе при изменении останутся устаревшие фрагменты.
- Чанки складываются в четыре параллельных буфера: обогащённый текст, физ. ключи, метаданные, исходный код.
- Счётчики: был `prev` хэш - обновление, иначе новый файл. Новый хэш пишется в реестр.

Шаг 2 - эмбеддинг батчами:
```python
    for i in range(0, len(texts), batch):
        sl = slice(i, i + batch)
        embs = embedder.encode(texts[sl], is_query=False)
        store.add(ids[sl], embs, metas[sl], codes[sl])
        embedded += len(ids[sl])
        _report()
```
- `encode(..., is_query=False)` - тексты как passage (важно для e5).
- `store.add(ids, embs, metas, codes)` - четыре параллельных списка. Код сохраняется, чтобы показывать результат без файлов.
- `_report` после каждого батча двигает прогресс-бар по чанкам; размер батча - компромисс throughput encode против частоты обновлений.

```python
    for rel in set(registry.files(source)) - current:
        store.delete_where(source=source, file=rel)
        registry.remove(source, rel)
    return {"added": added, "updated": updated, "skipped": skipped, "total": store.count()}
```
- Разница «что было в реестре» минус «что есть сейчас» = удалённые файлы: их чанки чистятся из стора, записи - из реестра. Возвращается сводка (показывается в админке/CLI).

### `remove_source(...)`
```python
def remove_source(source, store, registry) -> dict:
    store.delete_where(source=source)
    registry.remove(source)
    return {"removed": source, "total": store.count()}
```
- Полное удаление источника: все его чанки из стора + все записи реестра. Используется кнопкой «Удалить» в админке.

Пайплайн принимает store/embedder/registry аргументами, а не создаёт их: dependency injection - пайплайн не знает, Chroma под ним или Qdrant, SQLite или Postgres; их собирает `factory`. Это делает функцию тестируемой и переносимой между профилями.
