# index.py - CLI индексации

```python
"""CLI индексации: python index.py <папка> [имя_источника]."""
import sys
from dotenv import load_dotenv
from src.factory import build

def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else "data/codebase"
    source = sys.argv[2] if len(sys.argv) > 2 else folder.rstrip("/").split("/")[-1]
    comp = build()
    stats = comp.backend.index(folder, source, incremental=True)
    print(stats)

if __name__ == "__main__":
    load_dotenv()
    main()
```
- `sys.argv[1]` - путь к папке с кодом; дефолт `data/codebase`, если не передан.
- `sys.argv[2]` - имя источника; если не передано, берётся имя последней папки пути (`rstrip("/")` убирает хвостовой слэш, `split("/")[-1]` - последний сегмент). Например `data/codebase` → источник `codebase`.
- `build()` собирает компоненты по конфигу (профиль/стор/эмбеддер из `config.yaml`).
- `comp.backend.index(...)` - индексация идёт через бэкенд-фасад (а не пайплайн напрямую), чтобы CLI шёл тем же путём, что UI/REST. `incremental=True` - повторный запуск переиндексирует только изменённое.
- Печать сводки (`added/updated/skipped/total`).
- `load_dotenv()` под `__main__` подхватывает `.env` (адреса сервисов, ключи) до сборки компонентов.

Запуск: `python index.py data/codebase` (или `make index`). Корень `data/codebase` должен быть таким, чтобы относительные пути совпадали с эталонами scorer (`gymhero/...`).
