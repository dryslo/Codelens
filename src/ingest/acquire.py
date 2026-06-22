"""Acquisition: получить код во временную папку для index_path.

Безопасность: zip-slip (пути вне каталога) и zip-bomb (лимит размера/числа файлов) для ZIP;
anti-SSRF (только github.com) и filter='data' для tar. Код репозитория НЕ исполняется - только
парсится AST в pipeline. Возвращает путь к временной папке; чистит её вызывающая сторона.
"""
import io
import re
import tarfile
import tempfile
import zipfile
from pathlib import Path

MAX_FILES = 20000
MAX_TOTAL_BYTES = 500 * 1024 * 1024          # распакованный размер ZIP
GITHUB_TIMEOUT = 60

_GH_RE = re.compile(r"^https?://github\.com/([^/]+)/([^/#?]+?)(?:\.git)?/?$")


def _new_tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="codelens-ingest-"))


def _check_no_slip(root: Path, name: str) -> None:
    rr = root.resolve()
    target = (root / name).resolve()
    if rr != target and rr not in target.parents:
        raise ValueError(f"zip-slip: путь вне каталога архива: {name!r}")


def from_zip(data: bytes, *, max_files: int = MAX_FILES, max_total: int = MAX_TOTAL_BYTES) -> Path:
    """Распаковать ZIP во временную папку с защитой от zip-slip/zip-bomb."""
    root = _new_tmp()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = [i for i in zf.infolist() if not i.is_dir()]
        if len(members) > max_files:
            raise ValueError(f"слишком много файлов в архиве: {len(members)} > {max_files}")
        total = 0
        for info in members:
            total += info.file_size
            if total > max_total:
                raise ValueError("zip-bomb: распакованный размер превышает лимит")
            _check_no_slip(root, info.filename)
            zf.extract(info, root)
    return root


def from_github(url: str, ref: str | None = None, *, timeout: int = GITHUB_TIMEOUT) -> Path:
    """Скачать публичный GitHub-репозиторий (tar.gz) во временную папку."""
    m = _GH_RE.match((url or "").strip())
    if not m:
        raise ValueError("ожидается публичный URL вида https://github.com/<owner>/<repo>")
    owner, repo = m.group(1), m.group(2)
    import requests
    refs = [ref] if ref else ["main", "master"]   # без ref - дефолтные ветки
    last = None
    for r in refs:
        tar_url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/{r}"
        resp = requests.get(tar_url, timeout=timeout)
        if resp.status_code == 200:
            return _extract_tar(resp.content)
        last = resp.status_code
    raise ValueError(f"не удалось скачать {owner}/{repo} "
                     f"(ref={ref or 'main/master'}, http={last})")


def _extract_tar(data: bytes) -> Path:
    root = _new_tmp()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        tf.extractall(root, filter="data")        # 3.12+: защита от traversal/спецфайлов
    # codeload кладёт всё в единственную подпапку <repo>-<ref>/ - она и есть корень
    subs = [p for p in root.iterdir() if p.is_dir()]
    return subs[0] if len(subs) == 1 else root
