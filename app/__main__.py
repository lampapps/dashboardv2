"""Command line: python -m app <command>."""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config, db, mailer, maintenance, settings, tick


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


def _cmd_admin(args: argparse.Namespace) -> int:
    from . import admin

    return admin.run(args.unit)


def _cmd_tick(args: argparse.Namespace) -> int:
    cfg = config.load_config()
    db.init_db()
    conn = db.connect()
    try:
        results = tick.run_once(conn, cfg)
    finally:
        conn.close()
    if args.verbose:
        for result in results:
            print(f"{result['name']}\t{result['seconds'] * 1000:.0f} ms\t{result['error'] or 'ok'}")
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
    print(f"{settings.DB_PATH}: reset to schema version {maintenance.reset_database()}")
    return 0


def _cmd_agent_add(args: argparse.Namespace) -> int:
    key = config.add_agent(args.name, grace_minutes=args.grace_minutes, notes=args.notes,
                           allowed_ip=args.allowed_ip, host=args.host)
    print(f"Agent '{args.name}' registered in {settings.AGENTS_PATH} (id {config.find_agent(args.name).id})")
    print(f"API key (shown once; set it as JABS_AGENT_KEY on the agent): {key}")
    return 0


def _cmd_agent_adopt(_args: argparse.Namespace) -> int:
    adopted = config.adopt_agents()
    print(f"gave an id to {len(adopted)} agent(s): {', '.join(adopted)}" if adopted else "every agent already has an id")
    return 0


def _cmd_agent_list(_args: argparse.Namespace) -> int:
    agents = config.load_agents()
    if not agents:
        print("no agents registered")
    for agent in agents:
        state = "enabled" if agent.enabled else "disabled"
        print(f"{agent.name}\t{agent.id}\t{agent.host or '-'}\t{state}\tgrace {agent.grace_minutes} min\t{agent.notes}")
    return 0


def _cmd_agent_rekey(args: argparse.Namespace) -> int:
    key = config.rekey_agent(args.name)
    print(f"Agent '{args.name}' has a new key; the old one no longer works.")
    print(f"API key (shown once; set it as JABS_AGENT_KEY on the agent): {key}")
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
    print("usage: python -m app agent {add,list,set,rename,rekey,enable,disable,remove,adopt} ...", file=sys.stderr)
    return 2


def _cmd_agent_set(args: argparse.Namespace) -> int:
    changes = {}
    if args.host is not None:
        changes["host"] = args.host
    if args.grace_minutes is not None:
        changes["grace_minutes"] = args.grace_minutes
    if args.notes is not None:
        changes["notes"] = args.notes
    if args.allowed_ip is not None:
        changes["allowed_ip"] = args.allowed_ip or None  # an empty value clears the restriction
    if args.alerts is not None:
        changes["offline_alerts"] = args.alerts
    if not changes:
        print("nothing to change; give at least one option (see agent set --help)", file=sys.stderr)
        return 2
    config.update_agent(args.name, **changes)
    print(f"Agent '{args.name}' updated: {', '.join(sorted(changes))}")
    return 0


def _cmd_agent_rename(args: argparse.Namespace) -> int:
    config.rename_agent(args.name, args.new_name)
    print(f"Agent '{args.name}' renamed to '{args.new_name}'; its history is stored under its id, so it carries over")
    return 0


def _cmd_agent_remove(args: argparse.Namespace) -> int:
    if not args.yes:
        print(f"agent remove deletes '{args.name}' from the registry; re-run with --yes to confirm", file=sys.stderr)
        return 2
    agent_id = config.find_agent(args.name).id
    config.remove_agent(args.name)
    print(f"Agent '{args.name}' removed; its key is rejected from now on")
    if args.purge_history:
        conn = db.connect()
        try:
            deleted = maintenance.delete_agent_history(conn, agent_id)
        finally:
            conn.close()
        print("history deleted: " + ", ".join(f"{kind} {count}" for kind, count in deleted.items()))
    return 0


def _cmd_orphans(_args: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        found = maintenance.orphans(conn, [a.id for a in config.load_agents()])
    finally:
        conn.close()
    for row in found["agents"]:
        print(f"agent\t{row['name']}\t{row['id']}\t{row['runs']} run(s)\tnot in agents.yaml; "
              "restore its entry (same id) to see it again")
    for row in found["jobs"]:
        print(f"job\t{row['agent']}\t{row['job_name']}\t{row['job_key']}\t{row['runs']} run(s)\tno longer reported by its agent; "
              f"`runs delete --agent {row['agent']} --job {row['job_key']} --yes` removes it")
    if not found["agents"] and not found["jobs"]:
        print("no orphaned history")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    maintenance.check_backup_file(args.file)
    if not args.yes:
        print(f"restore replaces the database with {args.file}; re-run with --yes to confirm", file=sys.stderr)
        return 2
    result = maintenance.restore(args.file)
    if result["safety_copy"]:
        print(f"current database saved to {result['safety_copy']}")
    print(f"{settings.DB_PATH}: restored (schema version {result['schema_version']})")
    return 0


def _cmd_vacuum(_args: argparse.Namespace) -> int:
    db.init_db()
    conn = db.connect()
    try:
        sizes = maintenance.vacuum(conn)
    finally:
        conn.close()
    before, after = sizes["before"], sizes["after"]
    print(f"database {maintenance.format_bytes(before['db'] + before['wal'])} -> {maintenance.format_bytes(after['db'] + after['wal'])}")
    return 0


def _cmd_check_db(_args: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        problems = maintenance.integrity_problems(conn)
        counts = maintenance.table_counts(conn)
        version = db.schema_version(conn)
    finally:
        conn.close()
    sizes = maintenance.file_sizes()
    print(f"{settings.DB_PATH}: schema version {version}, {maintenance.format_bytes(sizes['db'])} (+ {maintenance.format_bytes(sizes['wal'])} WAL)")
    for table, rows in counts:
        print(f"  {table}\t{rows}")
    for problem in problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    print("integrity: " + ("problems found" if problems else "ok"))
    return 1 if problems else 0


def _cmd_meta(_args: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        for key, value in maintenance.meta_items(conn):
            print(f"{key}\t{value}")
    finally:
        conn.close()
    return 0


def _cmd_runs_usage(_args: argparse.Namespace) -> int:
    print("usage: python -m app runs {list,delete} ...", file=sys.stderr)
    return 2


def _cmd_runs_list(args: argparse.Namespace) -> int:
    agent_id = config.find_agent(args.agent).id
    conn = db.connect()
    try:
        job_key = maintenance.resolve_job(conn, agent_id, args.job) if args.job is not None else None
        runs = maintenance.list_runs(conn, agent_id, job_key, args.limit)
    finally:
        conn.close()
    for run in runs:
        started = datetime.fromtimestamp(run["started_at"]).strftime("%Y-%m-%d %H:%M:%S")
        runtime = "-" if run["runtime_seconds"] is None else f"{run['runtime_seconds']:.0f}s"
        print(f"{run['id']}\t{started}\t{run['status']}\t{run['job_name']}\t{run['target_label'] or '-'}\t{runtime}\t"
              f"{' '.join((run['message'] or '-').split())}")
    if not runs:
        print("no runs found")
    return 0


def _cmd_runs_delete(args: argparse.Namespace) -> int:
    if args.id and (args.agent or args.job is not None):
        print("use either --id (specific runs) or --agent [--job] (everything of an agent or job), not both", file=sys.stderr)
        return 2
    if not args.id and not args.agent:
        print("give --id RUN_ID (repeatable; see 'runs list') or --agent NAME [--job JOB]", file=sys.stderr)
        return 2
    scope = (f"run(s) {', '.join(str(i) for i in args.id)}" if args.id else
             f"agent '{args.agent}'" + (f", job '{args.job}'" if args.job is not None else ""))
    if not args.yes:
        print(f"runs delete removes stored runs and events for {scope}; re-run with --yes to confirm", file=sys.stderr)
        return 2
    conn = db.connect()
    try:
        if args.id:
            deleted = maintenance.delete_run_ids(conn, args.id)
        else:
            agent_id = config.find_agent(args.agent).id
            job_key = maintenance.resolve_job(conn, agent_id, args.job) if args.job is not None else None
            deleted = maintenance.delete_runs(conn, agent_id, job_key)
    finally:
        conn.close()
    print(f"deleted {deleted} run(s) for {scope}")
    return 0


def _cmd_storage_usage(_args: argparse.Namespace) -> int:
    print("usage: python -m app storage purge [--source SOURCE] [--name NAME] --yes", file=sys.stderr)
    return 2


def _cmd_storage_purge(args: argparse.Namespace) -> int:
    if not args.yes:
        print("storage purge deletes stored storage samples; re-run with --yes to confirm", file=sys.stderr)
        return 2
    conn = db.connect()
    try:
        source = args.source
        if source and source != "dashboard":
            source = {a.name: a.id for a in config.load_agents()}.get(source, source)  # samples are stored under the id
        deleted = maintenance.purge_storage(conn, source, args.name)
    finally:
        conn.close()
    print(f"deleted {deleted} storage sample(s)")
    return 0


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
    add("admin", _cmd_admin, "open the interactive administration console").add_argument(
        "--unit", help="systemd unit name of this install, if it cannot be detected (e.g. jabs-dashboard-v2)")
    add("tick", _cmd_tick, "run the periodic tasks once (driven by the systemd timer)").add_argument(
        "-v", "--verbose", action="store_true", help="print each task's duration and result")
    add("testmail", _cmd_testmail, "send a test message with the email settings")
    add("backup", _cmd_backup, "write a consistent copy of the database").add_argument(
        "--dest", type=Path, help="output file (default: data/backups/jabs-<timestamp>.sqlite)")
    restore = add("restore", _cmd_restore, "replace the database with a backup (the current one is saved first)")
    restore.add_argument("file", type=Path)
    restore.add_argument("--yes", action="store_true", help="confirm replacing the database")
    add("vacuum", _cmd_vacuum, "reclaim free space in the database file")
    add("check-db", _cmd_check_db, "run the integrity check and show size and row counts")
    add("meta", _cmd_meta, "show the internal key/value state (last tick, digest time, revision)")
    add("orphans", _cmd_orphans, "list stored history that nothing configures any more (removed agents and jobs)")
    add("reset", _cmd_reset, "delete the database and recreate it empty").add_argument(
        "--yes", action="store_true", help="confirm deleting all history")

    runs = add("runs", _cmd_runs_usage, "manage stored runs")
    runs_sub = runs.add_subparsers(dest="runs_command", metavar="subcommand")
    runs_list = runs_sub.add_parser("list", help="list an agent's stored runs with their ids, newest first")
    runs_list.add_argument("--agent", required=True)
    runs_list.add_argument("--job", help="only this job")
    runs_list.add_argument("--limit", type=int, default=50, help="how many to show (default 50)")
    runs_list.set_defaults(func=_cmd_runs_list)
    runs_delete = runs_sub.add_parser(
        "delete", help="delete specific runs by id, or all of an agent's (or one job's) runs")
    runs_delete.add_argument("--id", type=int, action="append", help="a run id (repeatable)")
    runs_delete.add_argument("--agent")
    runs_delete.add_argument("--job", help="only this job (default: all the agent's runs)")
    runs_delete.add_argument("--yes", action="store_true", help="confirm deleting")
    runs_delete.set_defaults(func=_cmd_runs_delete)

    storage = add("storage", _cmd_storage_usage, "manage stored storage samples")
    storage_purge = storage.add_subparsers(dest="storage_command", metavar="subcommand").add_parser(
        "purge", help="delete storage samples, all or for one source/name")
    storage_purge.add_argument("--source", help="'dashboard' or an agent name")
    storage_purge.add_argument("--name", help="drive, bucket or repository name")
    storage_purge.add_argument("--yes", action="store_true", help="confirm deleting")
    storage_purge.set_defaults(func=_cmd_storage_purge)

    agent = add("agent", _cmd_agent_usage, "manage registered agents")
    agent_sub = agent.add_subparsers(dest="agent_command", metavar="subcommand")
    agent_add = agent_sub.add_parser("add", help="register an agent and print its API key once")
    agent_add.add_argument("name")
    agent_add.add_argument("--host", default="", help="machine the agent runs on, shown beside its jobs (default: the agent name)")
    agent_add.add_argument("--grace-minutes", type=int, default=60,
                           help="minutes without contact before the agent counts as down")
    agent_add.add_argument("--notes", default="")
    agent_add.add_argument("--allowed-ip", help="only accept this agent's key from this address")
    agent_add.set_defaults(func=_cmd_agent_add)
    agent_sub.add_parser("list", help="list registered agents").set_defaults(func=_cmd_agent_list)
    agent_set = agent_sub.add_parser("set", help="change an agent's settings")
    agent_set.add_argument("name")
    agent_set.add_argument("--host", help="machine the agent runs on (empty: use the agent name)")
    agent_set.add_argument("--grace-minutes", type=int, help="minutes without contact before the agent counts as down")
    agent_set.add_argument("--notes")
    agent_set.add_argument("--allowed-ip", help="only accept the key from this address (empty: any address)")
    agent_set.add_argument("--alerts", action=argparse.BooleanOptionalAction, help="send offline alert emails")
    agent_set.set_defaults(func=_cmd_agent_set)
    agent_rename = agent_sub.add_parser(
        "rename", help="change an agent's name, keeping its key, settings and stored history")
    agent_rename.add_argument("name")
    agent_rename.add_argument("new_name")
    agent_rename.set_defaults(func=_cmd_agent_rename)
    agent_remove = agent_sub.add_parser("remove", help="delete an agent from the registry")
    agent_remove.add_argument("name")
    agent_remove.add_argument("--purge-history", action="store_true", help="also delete its stored runs, status and samples")
    agent_remove.add_argument("--yes", action="store_true", help="confirm removing")
    agent_remove.set_defaults(func=_cmd_agent_remove)
    rekey = agent_sub.add_parser("rekey", help="issue a new API key for an agent, keeping its name and history")
    rekey.add_argument("name")
    rekey.set_defaults(func=_cmd_agent_rekey)
    agent_sub.add_parser("adopt", help="give every agents.yaml entry that has no id a new one").set_defaults(
        func=_cmd_agent_adopt)
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
