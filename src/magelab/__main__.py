"""
CLI entry point for magelab.

Usage:
    uv run magelab config.yaml [--output-dir DIR] [--docker] [--docker-build]
    uv run magelab config.yaml --runs 5 --max-concurrent 2
    uv run magelab --output-dir DIR --view myorg.db
    uv run magelab --output-dir DIR --view-batch myorg.db

View mode opens a read-only frontend dashboard from a completed run's DB.
--view serves a single DB at output-dir/db_name. --view-batch finds db_name
in each subdirectory of output-dir (the batch layout: dir/timestamp/db_name)
and serves each on its own port starting from --frontend-port.
"""

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from .auth import resolve_api_key, resolve_sub
from .org_config import ResumeMode
from .pipeline import run_pipeline, run_pipeline_batch, view_run, view_run_batch
from .state.database import Database
from .state.database_hydration import reconstruct_org_config_from_db


def main() -> None:
    """CLI entry point: uv run magelab config.yaml [--output-dir DIR] [--docker]"""
    parser = argparse.ArgumentParser(
        prog="magelab",
        description="Run a multi-agent organization from a YAML config.",
    )
    parser.add_argument("config", nargs="?", default=None, help="Path to YAML config file")
    parser.add_argument(
        "-o", "--output-dir", type=Path, default=None, help="Output directory (default: {name}/{timestamp}/)"
    )
    parser.add_argument(
        "-d",
        "--docker",
        action="store_const",
        const="run",
        default=None,
        help="Run org phases inside a Docker container",
    )
    parser.add_argument(
        "-D",
        "--docker-build",
        action="store_const",
        const="build",
        dest="docker",
        help="Force rebuild Docker image, then run in Docker",
    )
    parser.add_argument(
        "--no-frontend",
        action="store_true",
        help="Disable the frontend dashboard (enabled by default)",
    )
    parser.add_argument(
        "--frontend-port",
        type=int,
        default=8765,
        help="Port for the frontend dashboard (default: 8765)",
    )
    parser.add_argument(
        "--resume",
        choices=["continue", "fresh"],
        default=None,
        help="Resume a previous run: 'continue' picks up where agents left off, "
        "'fresh' fails in-progress tasks and starts clean",
    )
    parser.add_argument(
        "--view",
        type=str,
        default=None,
        metavar="DB_NAME",
        help="Open a read-only frontend for a completed run (DB filename in --output-dir, e.g. myorg.db)",
    )
    parser.add_argument(
        "--view-batch",
        type=str,
        default=None,
        metavar="DB_NAME",
        help="Open read-only frontends for all runs in a batch (finds DB_NAME in each --output-dir subdirectory)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Number of runs to execute concurrently (default: 1)",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=1,
        help="Max concurrent runs when --runs > 1 (default: 1)",
    )
    auth_group = parser.add_mutually_exclusive_group()
    auth_group.add_argument(
        "--sub",
        nargs="?",
        const=True,
        default=None,
        metavar="CREDENTIALS_PATH",
        help="Use Claude subscription auth. Alone: auto-detect credentials. With path: use that .credentials.json.",
    )
    auth_group.add_argument(
        "--api-key",
        nargs="?",
        const=True,
        default=None,
        metavar="ENV_FILE",
        help="Use API key auth. Alone: read ANTHROPIC_API_KEY from env. With path: load that .env file.",
    )
    args = parser.parse_args()

    # ── Case 1: View mode ──────────────────────────────────────────────
    view_mode = args.view or args.view_batch
    if view_mode:
        if args.no_frontend:
            parser.error("--view/--view-batch requires the frontend (cannot use --no-frontend)")
        if not args.output_dir:
            parser.error("--view/--view-batch requires --output-dir")
        if args.resume or args.docker or args.runs > 1 or args.config:
            parser.error("--view/--view-batch cannot be combined with config, --resume, --docker, or --runs")
        if args.view and args.view_batch:
            parser.error("--view and --view-batch are mutually exclusive")

        frontend_port = args.frontend_port
        base_dir = args.output_dir
        if args.view:
            db_path = base_dir / args.view
            if not db_path.exists():
                parser.error(f"Database not found: {db_path}")
            view_run(db_path, frontend_port=frontend_port)
        else:
            db_files = sorted(base_dir.glob(f"*/{args.view_batch}"))
            if not db_files:
                parser.error(f"No {args.view_batch} files found in subdirectories of {base_dir}")
            view_run_batch(db_files, base_frontend_port=frontend_port)
        return

    # ── Resolve auth (required for run/resume) ─────────────────────────
    if args.sub is not None:
        auth = resolve_sub(Path(args.sub) if isinstance(args.sub, str) else None)
    elif args.api_key is not None:
        auth = resolve_api_key(Path(args.api_key) if isinstance(args.api_key, str) else None)
    else:
        parser.error("Authentication required: use --sub (subscription) or --api-key (API key)")

    # ── Case 2: Fresh run — config required ─────────────────────────────
    if not args.resume:
        if not args.config:
            parser.error("config is required for a fresh run")

        if args.output_dir:
            base_dir = args.output_dir
        else:
            with open(args.config) as f:
                raw = yaml.safe_load(f)
                config_name = raw.get("settings", {}).get("org_name", Path(args.config).stem)
            base_dir = Path.cwd() / config_name

        frontend_port = None if args.no_frontend else args.frontend_port
        now = datetime.now()

        if args.runs > 1:
            output_dirs = [base_dir / (now + timedelta(seconds=i)).strftime("%Y%m%d_%H%M%S") for i in range(args.runs)]
            batch_outcomes = asyncio.run(
                run_pipeline_batch(
                    config_path=args.config,
                    stages=None,
                    output_dirs=output_dirs,
                    max_concurrent=args.max_concurrent,
                    base_frontend_port=frontend_port,
                    docker=args.docker,
                    auth=auth,
                )
            )
            outcomes = [o for run in batch_outcomes for o in run]
            print(f"\nDone. Output: {base_dir}\n")
        else:
            output_dir = base_dir if args.output_dir else base_dir / now.strftime("%Y%m%d_%H%M%S")
            outcomes = asyncio.run(
                run_pipeline(
                    config_path=args.config,
                    stages=[None, None],
                    output_dir=output_dir,
                    frontend_port=frontend_port,
                    docker=args.docker,
                    auth=auth,
                )
            )
            print(f"\nDone. Output: {output_dir}\n")

        sys.exit(max(o.exit_code for o in outcomes) if outcomes else 1)

    # ── Case 3: Resume — config optional, output-dir required ─────────
    if not args.output_dir:
        parser.error("--resume requires --output-dir")
    if args.runs > 1:
        parser.error("--runs cannot be combined with --resume")

    base_dir = args.output_dir
    resume_mode = ResumeMode(args.resume)
    frontend_port = None if args.no_frontend else args.frontend_port

    # If no config provided, reconstruct from DB
    if args.config:
        config_path = args.config
    else:
        db_files = list(base_dir.glob("*.db"))
        if not db_files:
            parser.error(f"No .db file found in {base_dir}")
        if len(db_files) > 1:
            parser.error(
                f"Multiple .db files in {base_dir}: {[f.name for f in db_files]}. Pass a config to disambiguate."
            )

        with Database(db_files[0]) as db:
            config = reconstruct_org_config_from_db(db)
            run_number = db.run_count()

        configs_dir = base_dir / "configs"
        configs_dir.mkdir(parents=True, exist_ok=True)
        config_path = str(configs_dir / f"{run_number:03d}_resume.yaml")
        with open(config_path, "w") as f:
            yaml.dump(config.to_dict(), f, default_flow_style=False, sort_keys=False, width=120, allow_unicode=True)

    outcomes = asyncio.run(
        run_pipeline(
            config_path=config_path,
            stages=[None, None],
            output_dir=base_dir,
            frontend_port=frontend_port,
            docker=args.docker,
            auth=auth,
            resume_mode=resume_mode,
        )
    )
    print(f"\nDone. Output: {base_dir}\n")
    sys.exit(max(o.exit_code for o in outcomes) if outcomes else 1)


if __name__ == "__main__":
    main()
