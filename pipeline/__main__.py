"""Command line entry point: python -m pipeline <command>.

Commands that spend money require --paid. Everything else is offline.
"""

import argparse
from pathlib import Path

from . import common as c


def main():
    parser = argparse.ArgumentParser(prog="python -m pipeline", description=__doc__)
    parser.add_argument("--workdir", type=Path, help="Use another folder for state/ and outputs/ (e.g. the pilot)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="Stage 1: read + profile every row, choose scope, init state (no model)")
    p.add_argument("--input", type=Path, default=c.FULL_CSV)
    p.add_argument("--core", type=int, default=100_000)
    p.add_argument("--extension", type=int, default=50_000)
    p.add_argument("--all", action="store_true", help="Scope = every eligible row of --input (arbitrary CSVs)")

    p = sub.add_parser("enrich", help="Stage 2 (PAID): label pending reviews with the model")
    p.add_argument("--paid", action="store_true", help="Required: confirms you intend to spend API money")
    p.add_argument("--upto", type=int, required=True, help="Process scope ranks below this (e.g. 150, 550, 10050, 150000)")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument("--cap", type=float, default=15.0, help="Hard spend cap in USD across all runs (ledger)")
    p.add_argument("--max-minutes", type=float)
    p.add_argument("--stop-after", type=int, help="Planned interruption after N batches (resume demo)")
    p.add_argument("--recovery", action="store_true",
                   help="Declared one-time recovery pass over quote_not_in_source quarantines (fuzzy source-span repair)")

    p = sub.add_parser("run", help="All stages (PAID): prepare if needed -> enrich -> verify -> group -> rank -> memo")
    p.add_argument("--paid", action="store_true")
    p.add_argument("--input", type=Path, default=c.FULL_CSV)
    p.add_argument("--all", action="store_true", help="Scope = every eligible row of --input")
    p.add_argument("--upto", type=int, default=10**9, help="Scope ranks below this (default: whole scope)")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--cap", type=float, default=15.0)
    p.add_argument("--verify-frac", type=float, default=0.02)
    p.add_argument("--verify-min", type=int, default=20)
    p.add_argument("--max-minutes", type=float)
    p.add_argument("--stop-after", type=int)

    p = sub.add_parser("export-grading", help="Write grading/ from saved state and run the course checker (offline)")
    p.add_argument("--before", required=True, help="checkpoint file name in state/checkpoints (interrupted run end)")
    p.add_argument("--after", required=True, help="checkpoint file name (resumed run end)")
    p.add_argument("--check-only", action="store_true")

    sub.add_parser("rerank", help="Rebuild ranking.csv + aggregates from saved membership and records (no model)")
    sub.add_parser("status", help="Show record counts, spend and the latest progress (offline)")

    p = sub.add_parser("load-db", help="Load saved outputs into the dashboard DB (DATABASE_URL in .env, else local SQLite)")
    p.add_argument("--database-url")
    sub.add_parser("tracker", help="Local live run tracker at http://127.0.0.1:8766 (offline)")
    sub.add_parser("label-golden", help="Open the local page to hand-label the golden 50 (human only)")
    sub.add_parser("golden-export", help="Write evals/golden_human_labels.csv from the saved labels")

    args = parser.parse_args()
    if args.workdir:
        c.configure(args.workdir)
    if args.command == "prepare":
        from . import prepare
        core, ext = (10**9, 0) if args.all else (args.core, args.extension)
        prepare.summary(prepare.run(args.input.resolve(), core, ext))
    elif args.command == "enrich":
        if not args.paid:
            raise SystemExit("enrich calls a paid API. Re-run with --paid to confirm.")
        from datetime import datetime
        from . import enrich
        run_id = ("recovery-" if args.recovery else "enrich-") + datetime.now().strftime("%Y%m%d-%H%M%S")
        enrich.Enricher(run_id=run_id, upto_rank=args.upto, batch_size=args.batch_size, workers=args.workers,
                        cap_usd=args.cap, max_minutes=args.max_minutes, stop_after_batches=args.stop_after,
                        recovery=args.recovery).run()
    elif args.command == "run":
        if not args.paid:
            raise SystemExit("run calls a paid API. Re-run with --paid to confirm.")
        from . import orchestrate
        orchestrate.run(input_csv=args.input.resolve(), upto=args.upto, workers=args.workers, cap=args.cap,
                        verify_frac=args.verify_frac, verify_min=args.verify_min, max_minutes=args.max_minutes,
                        stop_after=args.stop_after, all_rows=args.all)
    elif args.command == "export-grading":
        from . import export
        export.check() if args.check_only else export.export(args.before, args.after)
    elif args.command == "rerank":
        from . import stages
        table = stages.rank()
        print(f"Ranking rebuilt from saved files (0 model calls): {len(table)} issues; top 5:")
        for row in table[:5]:
            print(f"  {row['rank']}. {row['issue_id']}  count={row['complaint_count']}  "
                  f"mean={row['mean_severity']}  score={row['priority_score']}")
    elif args.command == "status":
        from . import status
        status.show()
    elif args.command == "load-db":
        from . import load_db
        load_db.load(args.database_url)
    elif args.command == "tracker":
        from . import tracker
        tracker.serve()
    elif args.command == "label-golden":
        from . import golden_tool
        golden_tool.serve()
    elif args.command == "golden-export":
        from . import golden_tool
        golden_tool.export()


if __name__ == "__main__":
    main()
