"""CLI индексации: python index.py <папка> [имя_источника]."""
import sys
from dotenv import load_dotenv
from src.factory import build


def main() -> None:
    """Индексирует папку инкрементально; имя источника - из argv или имени папки."""
    folder = sys.argv[1] if len(sys.argv) > 1 else "data/codebase"
    source = sys.argv[2] if len(sys.argv) > 2 else folder.rstrip("/").split("/")[-1]
    comp = build()
    stats = comp.backend.index(folder, source, incremental=True)
    print(stats)


if __name__ == "__main__":
    load_dotenv()
    main()
