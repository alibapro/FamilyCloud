"""Управление FamilyCloud из командной строки.

  python cli.py adduser alibek [--admin]   создать пользователя
  python cli.py passwd alibek              сменить пароль (все устройства выйдут)
  python cli.py users                      список пользователей
  python cli.py devices                    список устройств
  python cli.py revoke 3                   отключить устройство (потерянный телефон)
  python cli.py scrub                      проверить все файлы и восстановить повреждённые
  python cli.py backup-db                  копия базы на второй диск
  python cli.py audit [N]                  последние события безопасности
  python cli.py serve                      запустить сервер
"""
import argparse
import getpass
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import config, db, maint, security  # noqa: E402
from app.storage import Storage  # noqa: E402


def ask_password() -> str:
    while True:
        p1 = getpass.getpass("Пароль (мин. 10 символов): ")
        p2 = getpass.getpass("Повторите пароль: ")
        if p1 != p2:
            print("Пароли не совпадают")
        elif len(p1) < security.MIN_PASSWORD:
            print("Слишком короткий")
        else:
            return p1


def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def main(argv=None):
    ap = argparse.ArgumentParser(description="FamilyCloud")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("adduser"); a.add_argument("username"); a.add_argument("--admin", action="store_true")
    a = sub.add_parser("passwd"); a.add_argument("username")
    sub.add_parser("users")
    sub.add_parser("devices")
    a = sub.add_parser("revoke"); a.add_argument("device_id", type=int)
    a = sub.add_parser("scrub"); a.add_argument("--no-repair", action="store_true")
    sub.add_parser("backup-db")
    a = sub.add_parser("audit"); a.add_argument("n", nargs="?", type=int, default=50)
    sub.add_parser("serve")
    args = ap.parse_args(argv)

    cfg = config.load(args.config)
    db.init(cfg)

    if args.cmd == "adduser":
        if not security.USERNAME_RE.match(args.username):
            sys.exit("Логин: латиница, цифры, _ . - (2–32 символа)")
        pw = ask_password()
        with db.conn(cfg) as c:
            if c.execute("SELECT 1 FROM users WHERE username=?", (args.username,)).fetchone():
                sys.exit("Такой пользователь уже есть")
            c.execute(
                "INSERT INTO users(username, pw_hash, folder, is_admin, created_at) VALUES(?,?,?,?,?)",
                (args.username, security.hash_password(pw), args.username.lower(), int(args.admin), int(time.time())),
            )
            db.audit(c, None, "user_created", "cli", args.username)
        (cfg.primary / args.username.lower()).mkdir(exist_ok=True)
        print(f"Пользователь {args.username} создан. Папка: {cfg.primary / args.username.lower()}")

    elif args.cmd == "passwd":
        pw = ask_password()
        with db.conn(cfg) as c:
            u = c.execute("SELECT id FROM users WHERE username=?", (args.username,)).fetchone()
            if not u:
                sys.exit("Нет такого пользователя")
            c.execute("UPDATE users SET pw_hash=? WHERE id=?", (security.hash_password(pw), u["id"]))
            c.execute("UPDATE devices SET revoked=1 WHERE user_id=?", (u["id"],))
            db.audit(c, u["id"], "password_changed", "cli")
        print("Пароль изменён, все устройства пользователя отключены.")

    elif args.cmd == "users":
        with db.conn(cfg) as c:
            for r in c.execute(
                "SELECT u.id, u.username, u.folder, COUNT(f.id) n, COALESCE(SUM(f.size),0) s "
                "FROM users u LEFT JOIN files f ON f.user_id=u.id GROUP BY u.id"
            ):
                print(f"{r['id']:>3}  {r['username']:<20} папка={r['folder']:<15} файлов={r['n']:<7} {r['s']/1024**3:.2f} ГБ")

    elif args.cmd == "devices":
        with db.conn(cfg) as c:
            for r in c.execute(
                "SELECT d.*, u.username FROM devices d JOIN users u ON u.id=d.user_id ORDER BY d.id"
            ):
                state = "ОТКЛЮЧЕНО" if r["revoked"] else "активно"
                print(f"{r['id']:>3}  {r['username']:<15} {r['name']:<20} {state:<10} посл.={fmt_ts(r['last_seen'])} ip={r['last_ip']}")

    elif args.cmd == "revoke":
        with db.conn(cfg) as c:
            c.execute("UPDATE devices SET revoked=1 WHERE id=?", (args.device_id,))
            db.audit(c, None, "device_revoked", "cli", str(args.device_id))
        print("Устройство отключено.")

    elif args.cmd == "scrub":
        store = Storage(cfg)
        print("Проверка всех файлов (может занять часы на больших дисках)...")
        stats = maint.scrub(cfg, store, repair=not args.no_repair)
        maint.backup_db(cfg)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        if stats["lost"]:
            sys.exit(2)

    elif args.cmd == "backup-db":
        maint.backup_db(cfg)
        print("Готово.")

    elif args.cmd == "audit":
        with db.conn(cfg) as c:
            rows = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (args.n,)).fetchall()
        for r in reversed(rows):
            print(f"{fmt_ts(r['ts'])}  {r['event']:<16} user={r['user_id']} ip={r['ip']} {r['detail']}")

    elif args.cmd == "serve":
        import uvicorn
        uvicorn.run("app.main:app", host=cfg.host, port=cfg.port, workers=1, proxy_headers=False, log_level="info")


if __name__ == "__main__":
    main()
