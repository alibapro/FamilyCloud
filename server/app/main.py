"""FamilyCloud — сервер семейного облака на своих дисках."""
import logging
import os
import re
import secrets
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import config, db, maint, security
from .storage import HEX64, Storage, safe_name, sha256_file

CFG = config.load()
db.init(CFG)
STORE = Storage(CFG)
LIMITER = security.Limiter(CFG.max_login_attempts, CFG.lockout_minutes * 60)

CFG.meta_dir.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("familycloud")
if not log.handlers:
    log.setLevel(logging.INFO)
    h = RotatingFileHandler(CFG.meta_dir / "server.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.addHandler(logging.StreamHandler())

_upload_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def upload_lock(upload_id: str) -> threading.Lock:
    with _locks_guard:
        return _upload_locks[upload_id]


# --- фоновое обслуживание -------------------------------------------------
_stop = threading.Event()


def _maintenance_loop():
    while not _stop.is_set():
        try:
            maint.retry_mirrors(CFG, STORE)
            maint.backup_db(CFG)
            maint.cleanup_stale_uploads(CFG, STORE)
        except Exception:
            log.exception("Ошибка обслуживания")
        _stop.wait(900)  # каждые 15 минут


@asynccontextmanager
async def lifespan(app):
    if os.environ.get("FAMILYCLOUD_NO_MAINT") != "1":
        threading.Thread(target=_maintenance_loop, daemon=True).start()
    log.info("Сервер запущен. Основной диск: %s, зеркало: %s", CFG.primary, CFG.mirror or "нет")
    yield
    _stop.set()


app = FastAPI(title="FamilyCloud", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


def client_ip(request: Request) -> str:
    ip = request.client.host if request.client else "?"
    if CFG.trust_proxy and ip in ("127.0.0.1", "::1"):
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()[:64]
    return ip


# --- аутентификация --------------------------------------------------------
@dataclass
class Principal:
    user_id: int
    username: str
    folder: str
    device_id: int
    ip: str


def auth(request: Request, authorization: str = Header(default="")) -> Principal:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Требуется вход")
    th = security.token_hash(authorization[7:].strip())
    now = int(time.time())
    ip = client_ip(request)
    with db.conn(CFG) as c:
        row = c.execute(
            "SELECT d.id AS did, d.expires_at, u.id AS uid, u.username, u.folder "
            "FROM devices d JOIN users u ON u.id = d.user_id WHERE d.token_hash=? AND d.revoked=0",
            (th,),
        ).fetchone()
        if not row or row["expires_at"] < now:
            raise HTTPException(401, "Сессия недействительна, войдите заново")
        c.execute("UPDATE devices SET last_seen=?, last_ip=? WHERE id=?", (now, ip, row["did"]))
    return Principal(row["uid"], row["username"], row["folder"], row["did"], ip)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    device_name: str = Field(default="phone", max_length=64)


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request):
    ip = client_ip(request)
    ukey, ikey = "u:" + body.username.lower(), "ip:" + ip
    if LIMITER.blocked(ukey) or LIMITER.blocked(ikey, limit=CFG.max_login_attempts * 4):
        raise HTTPException(429, f"Слишком много попыток. Подождите {CFG.lockout_minutes} мин.")
    with db.conn(CFG) as c:
        u = c.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone()
        ok = security.verify_password(u["pw_hash"] if u else security.DUMMY_HASH, body.password)
        if not (u and ok):
            LIMITER.fail(ukey)
            LIMITER.fail(ikey)
            db.audit(c, u["id"] if u else None, "login_failed", ip, body.username)
            log.warning("Неудачный вход: %s с %s", body.username, ip)
            raise HTTPException(401, "Неверный логин или пароль")
        LIMITER.reset(ukey)
        token, th = security.new_token()
        now = int(time.time())
        expires = now + CFG.token_days * 86400
        c.execute(
            "INSERT INTO devices(user_id, name, token_hash, created_at, expires_at, last_seen, last_ip) VALUES(?,?,?,?,?,?,?)",
            (u["id"], body.device_name, th, now, expires, now, ip),
        )
        db.audit(c, u["id"], "login", ip, body.device_name)
    log.info("Вход: %s (%s) с %s", u["username"], body.device_name, ip)
    return {"token": token, "username": u["username"], "expires_at": expires}


@app.post("/api/auth/logout")
def logout(p: Principal = Depends(auth)):
    with db.conn(CFG) as c:
        c.execute("UPDATE devices SET revoked=1 WHERE id=?", (p.device_id,))
        db.audit(c, p.user_id, "logout", p.ip)
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/me")
def me(p: Principal = Depends(auth)):
    with db.conn(CFG) as c:
        r = c.execute("SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM files WHERE user_id=?", (p.user_id,)).fetchone()
    return {
        "username": p.username,
        "files": r["n"],
        "bytes": r["s"],
        "free_bytes": STORE.free_bytes(),
        "mirror": STORE.mirror is not None,
    }


# --- загрузка с докачкой ---------------------------------------------------
class InitIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    taken_at: int | None = None  # мс с 1970


def _get_upload(c, p: Principal, upload_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise HTTPException(404, "Загрузка не найдена")
    up = c.execute("SELECT * FROM uploads WHERE id=? AND user_id=?", (upload_id, p.user_id)).fetchone()
    if not up:
        raise HTTPException(404, "Загрузка не найдена")
    return up


@app.post("/api/uploads")
def init_upload(body: InitIn, p: Principal = Depends(auth)):
    sha = body.sha256.lower()
    if not HEX64.match(sha):
        raise HTTPException(400, "Неверный sha256")
    if body.size > CFG.max_file_bytes:
        raise HTTPException(413, "Файл слишком большой")
    with db.conn(CFG) as c:
        f = c.execute("SELECT id FROM files WHERE user_id=? AND sha256=?", (p.user_id, sha)).fetchone()
        if f:
            return {"status": "exists", "file_id": f["id"]}
        up = c.execute(
            "SELECT id FROM uploads WHERE user_id=? AND sha256=? AND size=?", (p.user_id, sha, body.size)
        ).fetchone()
        if up:
            return {"status": "pending", "upload_id": up["id"], "offset": STORE.part_size(up["id"])}
        if STORE.free_bytes() - body.size < CFG.min_free_bytes:
            log.error("Мало места на дисках")
            raise HTTPException(507, "На сервере заканчивается место")
        uid = secrets.token_hex(16)
        c.execute(
            "INSERT INTO uploads(id, user_id, filename, size, sha256, taken_at, created_at) VALUES(?,?,?,?,?,?,?)",
            (uid, p.user_id, body.filename, body.size, sha, body.taken_at, int(time.time())),
        )
    STORE.part_path(uid).touch()
    return {"status": "pending", "upload_id": uid, "offset": 0}


@app.get("/api/uploads/{upload_id}")
def upload_status(upload_id: str, p: Principal = Depends(auth)):
    with db.conn(CFG) as c:
        up = _get_upload(c, p, upload_id)
    return {"offset": STORE.part_size(up["id"]), "size": up["size"]}


@app.put("/api/uploads/{upload_id}")
async def put_chunk(upload_id: str, offset: int, request: Request, p: Principal = Depends(auth)):
    cl = request.headers.get("content-length")
    if cl and int(cl) > CFG.chunk_max_bytes:
        raise HTTPException(413, "Слишком большой кусок")
    with db.conn(CFG) as c:
        up = _get_upload(c, p, upload_id)
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > CFG.chunk_max_bytes:
            raise HTTPException(413, "Слишком большой кусок")

    def write():
        with upload_lock(upload_id):
            part = STORE.part_path(upload_id)
            cur = part.stat().st_size if part.exists() else 0
            if offset != cur:
                return cur, False
            if cur + len(data) > up["size"]:
                raise HTTPException(400, "Данных больше, чем заявленный размер")
            with open(part, "ab") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            return cur + len(data), True

    new_offset, ok = await run_in_threadpool(write)
    if not ok:
        return JSONResponse(status_code=409, content={"detail": "Смещение не совпадает", "offset": new_offset})
    return {"offset": new_offset}


@app.post("/api/uploads/{upload_id}/complete")
def complete_upload(upload_id: str, p: Principal = Depends(auth)):
    with db.conn(CFG) as c:
        up = _get_upload(c, p, upload_id)
    with upload_lock(upload_id):
        part = STORE.part_path(upload_id)
        size = part.stat().st_size if part.exists() else 0
        if size != up["size"]:
            return JSONResponse(status_code=409, content={"detail": "Файл загружен не полностью", "offset": size})
        actual = sha256_file(part)
        if actual != up["sha256"]:
            part.unlink(missing_ok=True)
            with db.conn(CFG) as c:
                c.execute("DELETE FROM uploads WHERE id=?", (upload_id,))
                db.audit(c, p.user_id, "hash_mismatch", p.ip, up["filename"])
            log.warning("Хеш не совпал: %s (%s)", up["filename"], p.username)
            raise HTTPException(422, "Контрольная сумма не совпала — файл будет загружен заново")

        with db.conn(CFG) as c:
            f = c.execute("SELECT id FROM files WHERE user_id=? AND sha256=?", (p.user_id, actual)).fetchone()
            if f:
                part.unlink(missing_ok=True)
                c.execute("DELETE FROM uploads WHERE id=?", (upload_id,))
                return {"status": "exists", "file_id": f["id"], "mirrored": True}

        rel = STORE.place(part, p.folder, up["filename"], up["taken_at"], actual)
        mirrored = False
        try:
            mirrored = STORE.mirror_copy(rel, actual)
        except Exception as e:
            log.error("Не удалось записать зеркало %s: %s (повторим позже)", rel, e)

        with db.conn(CFG) as c:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute(
                "INSERT INTO files(user_id, sha256, size, rel_path, original_name, taken_at, created_at, mirrored, last_verified) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (p.user_id, actual, up["size"], rel, safe_name(up["filename"]), up["taken_at"], int(time.time()), int(mirrored), int(time.time())),
            )
            c.execute("DELETE FROM uploads WHERE id=?", (upload_id,))
            c.execute("COMMIT")
            file_id = cur.lastrowid
    with _locks_guard:
        _upload_locks.pop(upload_id, None)
    return {"status": "stored", "file_id": file_id, "mirrored": mirrored or STORE.mirror is None}


# --- проверка перед удалением с телефона ------------------------------------
class VerifyIn(BaseModel):
    hashes: list[str] = Field(max_length=5000)
    deep: bool = False  # True = перечитать файлы и пересчитать SHA-256 (медленно)


@app.post("/api/files/verify")
def verify(body: VerifyIn, p: Principal = Depends(auth)):
    """Файл считается «безопасным», только если он есть на основном диске И на зеркале (если оно настроено)."""
    safe, unsafe = [], []
    with db.conn(CFG) as c:
        for h in body.hashes:
            h = h.lower()
            row = HEX64.match(h) and c.execute(
                "SELECT id, rel_path, size, mirrored FROM files WHERE user_id=? AND sha256=?", (p.user_id, h)
            ).fetchone()
            if not row:
                unsafe.append(h)
                continue
            sha = h if body.deep else None
            ok = STORE.copy_ok(STORE.primary, row["rel_path"], row["size"], sha)
            if ok and STORE.mirror is not None:
                if not row["mirrored"]:
                    try:
                        STORE.mirror_copy(row["rel_path"], h)
                        c.execute("UPDATE files SET mirrored=1 WHERE id=?", (row["id"],))
                    except Exception as e:
                        log.warning("Зеркало недоступно: %s", e)
                ok = STORE.copy_ok(STORE.mirror, row["rel_path"], row["size"], sha)
            (safe if ok else unsafe).append(h)
        db.audit(c, p.user_id, "verify", p.ip, f"safe={len(safe)} unsafe={len(unsafe)}")
    return {"safe": safe, "unsafe": unsafe}


# --- просмотр и скачивание своих файлов -----------------------------------
@app.get("/api/files")
def list_files(after_id: int = 0, limit: int = 200, p: Principal = Depends(auth)):
    limit = max(1, min(limit, 1000))
    with db.conn(CFG) as c:
        rows = c.execute(
            "SELECT id, original_name, size, sha256, taken_at, created_at, rel_path FROM files "
            "WHERE user_id=? AND id>? ORDER BY id LIMIT ?",
            (p.user_id, after_id, limit),
        ).fetchall()
    return {"files": [dict(r) for r in rows]}


@app.get("/api/files/{file_id}/download")
def download(file_id: int, p: Principal = Depends(auth)):
    with db.conn(CFG) as c:
        r = c.execute("SELECT * FROM files WHERE id=? AND user_id=?", (file_id, p.user_id)).fetchone()
    if not r:
        raise HTTPException(404, "Файл не найден")
    for root in (STORE.primary, STORE.mirror):
        if STORE.copy_ok(root, r["rel_path"], r["size"]):
            return FileResponse(STORE.resolve(root, r["rel_path"]), filename=r["original_name"])
    raise HTTPException(503, "Файл временно недоступен (диск отключён?)")
