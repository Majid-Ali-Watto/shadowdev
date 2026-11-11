"""Usage:
    python -m shadowdev            # the server: web page at PUBLIC_URL, Jira webhook at /webhook
    python -m shadowdev --reload   # the same, restarted whenever ShadowDev's code or .env changes (development)
    python -m shadowdev dry-run --summary "..." [--description "..."] [--type Bug|Task] [--note "..."]
                                [--project NAME] [--as DEVELOPER]
                                   # try the pipeline on a made-up issue; nothing goes to Jira or GitHub
"""
import argparse
import logging
from pathlib import Path

from .config import PROJECT_ROOT, Config, read_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m shadowdev")
    parser.add_argument("--reload", action="store_true",
                        help="restart the server whenever ShadowDev's code or .env changes (for development)")
    commands = parser.add_subparsers(dest="command")
    dry = commands.add_parser("dry-run", help="run the pipeline on a made-up issue, without Jira or GitHub")
    dry.add_argument("--summary", required=True, help="the made-up issue's title")
    text = dry.add_mutually_exclusive_group()
    text.add_argument("--description", default="", help="the made-up issue's description")
    text.add_argument("--description-file", type=Path, help="read the description from a file")
    dry.add_argument("--type", default="Bug", choices=["Bug", "Task", "Story"], help="issue type (default Bug)")
    dry.add_argument("--note", default="", help="a developer's note for Claude, as on the Run issues now form")
    dry.add_argument("--project", help="project name, e.g. marketplace (default: your only one)")
    dry.add_argument("--as", dest="who", help="developer: name, GitHub login or Jira account ID (default: the only one)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s %(message)s")
    if args.reload:
        if args.command:
            parser.error("--reload is for the server only")
        from .reload import watch

        return watch([])  # the server itself runs in a child process, restarted on changes
    cfg = Config.from_env(read_settings(PROJECT_ROOT / ".env"))
    if args.command == "dry-run":
        from .app import database
        from .automation.dryrun import dry_run
        from .repositories.projects import ProjectRepository
        from .repositories.users import UserRepository

        description = args.description_file.read_text() if args.description_file else args.description
        db = database(cfg)
        dry_run(cfg, UserRepository(db), ProjectRepository(db), args.project, args.who,
                args.type, args.summary, description, args.note.strip())
    else:
        from .server import serve

        serve(cfg)


if __name__ == "__main__":
    main()
