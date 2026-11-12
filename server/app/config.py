"""Загрузка настроек из config.toml."""
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    host: str
    port: int
    primary: Path            # основной диск
    mirror: Path | None      # второй диск (зеркальная копия) или None
    db_path: Path
    token_days: int
    max_login_attempts: int
    lockout_minutes: int
    max_file_bytes: int
    chunk_max_bytes: int
    min_free_bytes: int
    trust_proxy: bool        # доверять X-Forwarded-For от 127.0.0.1 (tailscale serve)

    @property
    def meta_dir(self) -> Path:
        return self.primary / ".familycloud"


def load(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("FAMILYCLOUD_CONFIG", "config.toml"))
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    srv = data.get("server", {})
    st = data["storage"]
    sec = data.get("security", {})

    primary = Path(st["primary"]).expanduser().resolve()
    mirror = Path(st["mirror"]).expanduser().resolve() if st.get("mirror") else None
    if mirror is not None and (mirror == primary or primary in mirror.parents or mirror in primary.parents):
        raise ValueError("Зеркало должно быть на другом диске/в другой папке, не внутри основной")

    primary.mkdir(parents=True, exist_ok=True)
    if mirror:
        mirror.mkdir(parents=True, exist_ok=True)

    db_path = Path(st.get("database") or (primary / ".familycloud" / "cloud.db"))
    return Config(
        host=srv.get("host", "127.0.0.1"),
        port=int(srv.get("port", 8080)),
        primary=primary,
        mirror=mirror,
        db_path=db_path,
        token_days=int(sec.get("token_days", 180)),
        max_login_attempts=int(sec.get("max_login_attempts", 5)),
        lockout_minutes=int(sec.get("lockout_minutes", 15)),
        max_file_bytes=int(float(sec.get("max_file_gb", 50)) * 1024**3),
        chunk_max_bytes=int(sec.get("chunk_max_mb", 16)) * 1024**2,
        min_free_bytes=int(float(st.get("min_free_gb", 5)) * 1024**3),
        trust_proxy=bool(srv.get("trust_proxy", True)),
    )
