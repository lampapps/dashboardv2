"""Command line: python -m app <command>."""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config, db, mailer, settings, tick


def _cmd_initdb(_args: argparse.Namespace) -> int:
    version = db.init_db()
    print(f"{settings.DB_PATH}: schema version {version}")
    return 0


def _cmd_config(_args: argparse.Namespace) -> int:
    cfg = config.load_config()
    agents = config.load_agents()
    print(f"{settings.CONFIG_PATH}: OK (timezone {cfg.timezone}, {len(cfg.badges)} badges)")
    note = "" if settings.AGENTS_PATH.exists() else " (file not found; none registered)"
    print(f"{settings.AGENTS_PATH}: OK ({len(agents)} agents){note}")
    return 0


def _cmd_web(args: argparse.Namespace) -> int:
    from .web.server import serve

    serve(dev=args.dev)
    return 0


def _cmd_tick(_args: argparse.Namespace) -> int:
    cfg = config.load_config()
    db.init_db()
    conn = db.connect()
    try:
        tick.run_once(conn, cfg)
    finally:
        conn.close()
    return 0


def _cmd_testmail(_args: argparse.Namespace) -> int:
    cfg = config.load_config()
    if not mailer.is_configured(cfg.email):
        print("email is not configured: set email.smtp_server and email.to_addrs in config.yaml", file=sys.stderr)
        return 1
    hint = mailer.smtp_login_hint(cfg.email)
    if hint:
        print(f"note: {hint}", file=sys.stderr)
    text, html = mailer.render(
        "test", server=cfg.email.smtp_server, port=cfg.email.smtp_port, recipients=", ".join(cfg.email.to_addrs),
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"))
    if not mailer.send(cfg.email, "JABS test message", text, html):
        print("sending failed; see the error above", file=sys.stderr)
        return 1
    print(f"test message sent to {', '.join(cfg.email.to_addrs)}")
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    dest = args.dest or settings.BACKUP_DIR / f"jabs-{datetime.now():%Y%m%d-%H%M%S}.sqlite"
    print(f"backup written to {db.backup(dest)}")
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    if not args.yes:
        print("reset deletes the database and all history; re-run with --yes to confirm", file=sys.stderr)
        return 2
    for suffix in ("", "-wal", "-shm"):
        Path(f"{settings.DB_PATH}{suffix}").unlink(missing_ok=True)
    print(f"{settings.DB_PATH}: reset to schema version {db.init_db()}")
    return 0


def _cmd_agent_add(args: argparse.Namespace) -> int:
    key = config.add_agent(args.name, grace_minutes=args.grace_minutes, notes=args.notes,
                           allowed_ip=args.allowed_ip)
    print(f"Agent '{args.name}' registered in {settings.AGENTS_PATH}")
    print(f"API key (shown once; set it as JABS_AGENT_KEY on the agent): {key}")
    return 0


def _cmd_agent_list(_args: argparse.Namespace) -> int:
    agents = config.load_agents()
    if not agents:
        print("no agents registered")
    for agent in agents:
        state = "enabled" if agent.enabled else "disabled"
        print(f"{agent.name}\t{state}\tgrace {agent.grace_minutes} min\t{agent.notes}")
    return 0


def _cmd_agent_enable(args: argparse.Namespace) -> int:
    config.set_agent_enabled(args.name, True)
    print(f"Agent '{args.name}' enabled")
    return 0


def _cmd_agent_disable(args: argparse.Namespace) -> int:
    config.set_agent_enabled(args.name, False)
    print(f"Agent '{args.name}' disabled; its key is rejected until re-enabled")
    return 0


def _cmd_agent_usage(_args: argparse.Namespace) -> int:
    print("usage: python -m app agent {add,list,enable,disable} ...", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app", description="JABS dashboard management")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    def add(name: str, func, help_text: str) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text, description=help_text)
        command.set_defaults(func=func)
        return command

    add("initdb", _cmd_initdb, "create or upgrade the database schema")
    add("config", _cmd_config, "validate config.yaml and agents.yaml")
    add("web", _cmd_web, "serve the web UI and agent API").add_argument(
        "--dev", action="store_true", help="Flask dev server instead of gunicorn")
    add("tick", _cmd_tick, "run the periodic tasks once (driven by the systemd timer)")
    add("testmail", _cmd_testmail, "send a test message with the email settings")
    add("backup", _cmd_backup, "write a consistent copy of the database").add_argument(
        "--dest", type=Path, help="output file (default: data/backups/jabs-<timestamp>.sqlite)")
    add("reset", _cmd_reset, "delete the database and recreate it empty").add_argument(
        "--yes", action="store_true", help="confirm deleting all history")

    agent = add("agent", _cmd_agent_usage, "manage registered agents")
    agent_sub = agent.add_subparsers(dest="agent_command", metavar="subcommand")
    agent_add = agent_sub.add_parser("add", help="register an agent and print its API key once")
    agent_add.add_argument("name")
    agent_add.add_argument("--grace-minutes", type=int, default=60,
                           help="minutes without contact before the agent counts as down")
    agent_add.add_argument("--notes", default="")
    agent_add.add_argument("--allowed-ip", help="only accept this agent's key from this address")
    agent_add.set_defaults(func=_cmd_agent_add)
    agent_sub.add_parser("list", help="list registered agents").set_defaults(func=_cmd_agent_list)
    for verb, func in (("enable", _cmd_agent_enable), ("disable", _cmd_agent_disable)):
        verb_parser = agent_sub.add_parser(verb, help=f"{verb} a registered agent")
        verb_parser.add_argument("name")
        verb_parser.set_defaults(func=func)
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    settings.load_env()
    level = logging.DEBUG if settings.env_mode() == "development" else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s - %(levelname)s - %(message)s")
    try:
        return args.func(args)
    except (config.ConfigError, db.DbError) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
