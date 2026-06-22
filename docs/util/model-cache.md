# util/model_cache.py - локальный кэш моделей

Чтобы тяжёлые модели (e5-large ~2.2 ГБ, bge-reranker ~2 ГБ) не качались при каждом старте.

```python
CACHE_DIR = Path(os.environ.get("MODEL_CACHE", "cache/models"))
```
- Путь кэша из переменной окружения `MODEL_CACHE`, по умолчанию `cache/models`. Почему свой кэш, а не дефолтный `~/.cache/huggingface`: проектный кэш переносим (можно положить рядом, смонтировать в контейнер, переопределить путь), не зависит от `$HOME` пользователя/контейнера.

```python
def _safe(name: str) -> str:
    return name.replace("/", "-")
```
- Имена моделей содержат `/` (`intfloat/multilingual-e5-large`), что в пути создало бы лишние подпапки. Замена на `-` → один каталог `intfloat-multilingual-e5-large`.

```python
def cached_sentence_transformer(name: str):
    from sentence_transformers import SentenceTransformer
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    local = CACHE_DIR / _safe(name)
    if local.exists():
        return SentenceTransformer(str(local))
    model = SentenceTransformer(name)
    model.save(str(local))
    return model
```
- Ленивый импорт `SentenceTransformer` - модуль тяжёлый, грузится только когда реально нужно (не при импорте утилиты).
- `mkdir(parents=True, exist_ok=True)` - создаёт `cache/models` со всеми родителями; `exist_ok` - не падать, если уже есть.
- `if local.exists()` - если папка модели уже на диске, загрузка идёт с диска (без сети). Иначе модель скачивается по имени из хаба и `model.save(local)` сохраняет в кэш для следующих запусков.
- Базовый паттерн, обёрнутый надёжно (создание каталога, безопасное имя).

```python
def cached_cross_encoder(name: str):
    ...
    return CrossEncoder(str(local)) ...
```
- То же для реранкера: `CrossEncoder` тоже умеет `.save()/загрузку по пути`.

Подводные камни: первый запуск всё равно скачивает (сеть нужна один раз); `.save()` пишет полную копию модели в `cache/models` (место на диске). `cache/` добавлен в `.gitignore`.
