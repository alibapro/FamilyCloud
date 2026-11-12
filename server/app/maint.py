"""Обслуживание: досоздание зеркал, резервная копия БД, проверка целостности (scrub)."""
import logging
import sqlite3
import time

from . import db
from .storage import Storage, copy_verified, sha256_file

log = logging.getLogger("familycloud")


def retry_mirrors(cfg, store: Storage) -> int:
    if store.mirror is None:
        return 0
    done = 0
    with db.conn(cfg) as c:
        rows = c.execute("SELECT id, rel_path, sha256 FROM files WHERE mirrored=0").fetchall()
        for r in rows:
            try:
                store.mirror_copy(r["rel_path"], r["sha256"])
                c.execute("UPDATE files SET mirrored=1 WHERE id=?", (r["id"],))
                done += 1
            except Exception as e:  # диск отключён и т.п. — попробуем позже
                log.warning("Зеркало для %s пока не создано: %s", r["rel_path"], e)
                break
    return done


def backup_db(cfg) -> None:
    """Копия базы на второй диск (или в .familycloud/backups, если зеркала нет)."""
    target_dir = (cfg.mirror / ".familycloud") if cfg.mirror else (cfg.meta_dir / "backups")
    target_dir.mkdir(parents=True, exist_ok=True)
    tmp = target_dir / "cloud.db.bak.tmp"
    src = db.connect(cfg)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    tmp.replace(target_dir / "cloud.db.bak")


def cleanup_stale_uploads(cfg, store: Storage, days: int = 14) -> int:
    """Удаляет незавершённые загрузки старше N дней (только временные .part, не файлы пользователей)."""
    cutoff = int(time.time()) - days * 86400
    n = 0
    with db.conn(cfg) as c:
        for r in c.execute("SELECT id FROM uploads WHERE created_at < ?", (cutoff,)).fetchall():
            p = store.part_path(r["id"])
            if p.exists():
                p.unlink()
            c.execute("DELETE FROM uploads WHERE id=?", (r["id"],))
            n += 1
    return n


def scrub(cfg, store: Storage, repair: bool = True) -> dict:
    """Перечитывает все файлы, сверяет SHA-256 и восстанавливает повреждённую копию из целой."""
    stats = {"ok": 0, "repaired": 0, "lost": 0, "lost_files": []}
    with db.conn(cfg) as c:
        rows = c.execute("SELECT id, rel_path, sha256, size FROM files ORDER BY id").fetchall()
        for r in rows:
            rel, sha, size = r["rel_path"], r["sha256"], r["size"]
            p_ok = store.copy_ok(store.primary, rel, size, sha)
            m_ok = store.copy_ok(store.mirror, rel, size, sha) if store.mirror else None
            if p_ok and m_ok in (True, None):
                stats["ok"] += 1
            elif p_ok and m_ok is False:
                if repair:
                    copy_verified(store.resolve(store.primary, rel), store.resolve(store.mirror, rel), sha)
                    c.execute("UPDATE files SET mirrored=1 WHERE id=?", (r["id"],))
                    log.warning("Восстановлена копия на зеркале: %s", rel)
                stats["repaired"] += 1
            elif not p_ok and m_ok:
                if repair:
                    copy_verified(store.resolve(store.mirror, rel), store.resolve(store.primary, rel), sha)
                    log.warning("Восстановлен файл на основном диске: %s", rel)
                stats["repaired"] += 1
            else:
                stats["lost"] += 1
                stats["lost_files"].append(rel)
                log.error("ПОВРЕЖДЕНЫ ОБЕ КОПИИ: %s", rel)
                continue
            c.execute("UPDATE files SET last_verified=? WHERE id=?", (int(time.time()), r["id"]))
    return stats


def verify_all_quick(cfg, store: Storage) -> None:
    """Быстрая проверка, что файлы на месте (по размеру), без чтения содержимого."""
    with db.conn(cfg) as c:
        for r in c.execute("SELECT rel_path, size FROM files").fetchall():
            if not store.copy_ok(store.primary, r["rel_path"], r["size"]):
                log.error("Файл отсутствует на основном диске: %s", r["rel_path"])


__all__ = ["retry_mirrors", "backup_db", "cleanup_stale_uploads", "scrub", "sha256_file"]
