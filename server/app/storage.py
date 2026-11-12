"""Работа с дисками: безопасные имена, атомарная запись, зеркало, проверка хешей."""
import hashlib
import os
import re
import secrets
import shutil
import time
import unicodedata
from datetime import datetime
from pathlib import Path

READ_CHUNK = 4 * 1024 * 1024
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def safe_name(name: str) -> str:
    """Имя файла без путей, запрещённых символов и зарезервированных имён Windows."""
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("\\", "/").split("/")[-1]
    name = _BAD.sub("_", name).strip(" .")
    if not name:
        name = "file"
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    if stem.upper() in _RESERVED:
        stem = "_" + stem
    stem, ext = stem[:120], ext[:16]
    return f"{stem}.{ext}" if ext else stem


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(READ_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def fsync_dir(path: Path) -> None:
    """На Linux фиксирует запись о файле в каталоге; на Windows не требуется."""
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_verified(src: Path, dst: Path, expected_sha: str) -> None:
    """Копирует во временный файл, сверяет SHA-256 и только потом атомарно ставит на место.
    Существующий dst (повреждённый) не удаляется, а переименовывается в *.corrupt-<время>."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.part-{secrets.token_hex(4)}")
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while chunk := fi.read(READ_CHUNK):
                h.update(chunk)
                fo.write(chunk)
            fo.flush()
            os.fsync(fo.fileno())
        if h.hexdigest() != expected_sha:
            raise IOError(f"Источник {src} не совпадает с эталонным хешем")
        if sha256_file(tmp) != expected_sha:
            raise IOError(f"Копия {tmp} повреждена при записи")
        if dst.exists():
            dst.rename(dst.with_name(f"{dst.name}.corrupt-{int(time.time())}"))
        os.replace(tmp, dst)
        fsync_dir(dst.parent)
    finally:
        if tmp.exists():
            tmp.unlink()


class Storage:
    def __init__(self, cfg):
        self.cfg = cfg
        self.primary: Path = cfg.primary
        self.mirror: Path | None = cfg.mirror
        self.tmp = cfg.meta_dir / "tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)

    # --- пути -------------------------------------------------------------
    @staticmethod
    def resolve(root: Path, rel: str) -> Path:
        p = (root / rel).resolve()
        if root.resolve() not in p.parents:
            raise PermissionError("Путь вне хранилища")
        return p

    def part_path(self, upload_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
            raise ValueError("bad upload id")
        return self.tmp / f"{upload_id}.part"

    def part_size(self, upload_id: str) -> int:
        p = self.part_path(upload_id)
        return p.stat().st_size if p.exists() else 0

    def _exists_anywhere(self, rel: Path) -> bool:
        return (self.primary / rel).exists() or (self.mirror is not None and (self.mirror / rel).exists())

    def _unique_rel(self, folder: str, name: str, taken_at_ms: int | None, sha: str) -> Path:
        ts = (taken_at_ms / 1000) if taken_at_ms else time.time()
        try:
            dt = datetime.fromtimestamp(ts)
        except (OverflowError, OSError, ValueError):
            dt = datetime.now()
        rel_dir = Path(folder) / f"{dt.year:04d}" / f"{dt.month:02d}"
        stem, dot, ext = name.rpartition(".")
        if not dot or not stem:
            stem, ext = name, ""
        suffix = f".{ext}" if ext else ""
        candidates = [name, f"{stem}_{sha[:8]}{suffix}"] + [f"{stem}_{sha[:8]}_{i}{suffix}" for i in range(2, 1000)]
        for cand in candidates:
            rel = rel_dir / cand
            if not self._exists_anywhere(rel):
                return rel
        raise IOError("Не удалось подобрать уникальное имя")

    # --- запись -----------------------------------------------------------
    def place(self, part: Path, folder: str, name: str, taken_at_ms: int | None, sha: str) -> str:
        """Перемещает проверенный .part в папку пользователя/год/месяц. Никогда не перезаписывает."""
        rel = self._unique_rel(folder, safe_name(name), taken_at_ms, sha)
        dest = self.primary / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, dest)  # один диск -> атомарно
        fsync_dir(dest.parent)
        return rel.as_posix()

    def mirror_copy(self, rel: str, sha: str) -> bool:
        if self.mirror is None:
            return False
        dst = self.resolve(self.mirror, rel)
        if dst.exists() and dst.stat().st_size and sha256_file(dst) == sha:
            return True
        copy_verified(self.resolve(self.primary, rel), dst, sha)
        return True

    # --- проверка ---------------------------------------------------------
    def copy_ok(self, root: Path | None, rel: str, size: int, sha: str | None = None) -> bool:
        if root is None:
            return False
        try:
            p = self.resolve(root, rel)
            if not p.is_file() or p.stat().st_size != size:
                return False
            return sha is None or sha256_file(p) == sha
        except OSError:
            return False

    def free_bytes(self) -> int:
        free = shutil.disk_usage(self.primary).free
        if self.mirror is not None:
            free = min(free, shutil.disk_usage(self.mirror).free)
        return free
