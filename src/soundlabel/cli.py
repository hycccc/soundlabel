"""soundlabel CLI: init a workspace, manage the roster, produce, browse.

``soundlabel demo`` is the two-minute tour: it builds a throwaway label,
signs an artist, and runs the full loop — A&R brief, mock generation,
gate + rank, blind Critic verdict, catalog entry.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .catalog import Artist, Catalog
from .pipeline import PaidBackendRefused, run_batch


def _workspace(args) -> Path:
    ws = Path(args.workspace)
    if not (ws / "catalog.db").exists() and args.cmd not in ("init", "demo"):
        sys.exit(f"no label workspace at {ws} — run `soundlabel init {ws}` first")
    return ws


def cmd_init(args) -> None:
    ws = Path(args.workspace)
    Catalog(ws / "catalog.db").close()
    (ws / "batches").mkdir(parents=True, exist_ok=True)
    print(f"label workspace ready at {ws.resolve()}")


def cmd_roster(args) -> None:
    catalog = Catalog(_workspace(args) / "catalog.db")
    if args.roster_cmd == "add":
        catalog.add_artist(Artist(args.slug, args.name or args.slug.title(),
                                  args.language, args.profile))
        print(f"signed: {args.slug}")
    else:
        for a in catalog.artists():
            profile = f" — {a.sonic_profile}" if a.sonic_profile else ""
            print(f"{a.slug:16s} {a.name} [{a.language}]{profile}")
    catalog.close()


def cmd_produce(args) -> None:
    anr = critic = None
    if args.llm:
        try:
            from .agents.llm import LLMANRAgent, LLMCriticAgent
        except ImportError as exc:
            sys.exit(str(exc))
        anr, critic = LLMANRAgent(), LLMCriticAgent()
    try:
        result = run_batch(_workspace(args), args.artist,
                           backend=args.backend, allow_paid=args.allow_paid,
                           anr_agent=anr, critic_agent=critic)
    except (KeyError, PaidBackendRefused) as exc:
        sys.exit(str(exc))
    _print_batch(result)
    if result.status == "failed":
        sys.exit(1)


def cmd_catalog(args) -> None:
    catalog = Catalog(_workspace(args) / "catalog.db")
    rows = catalog.tracks()
    if not rows:
        print("catalog is empty")
    for r in rows:
        print(f"{r['id']}  {r['score']:>5.2f}  {r['artist_slug']:12s}  {r['title']}")
    catalog.close()


def cmd_demo(args) -> None:
    ws = Path(args.workspace)
    catalog = Catalog(ws / "catalog.db")
    catalog.add_artist(Artist("june-holiday", "June Holiday", "en",
                              "warm pop and rnb, late-night ballads"))
    catalog.close()
    print("── demo label ─────────────────────────────────────")
    print("signed: june-holiday (June Holiday)")
    for i in range(args.tracks):
        result = run_batch(ws, "june-holiday", backend="mock")
        _print_batch(result)
    print("── catalog ───────────────────────────────────────")
    args.workspace = str(ws)
    args.cmd = "catalog"
    cmd_catalog(args)


def _print_batch(result) -> None:
    print(f"\n[{result.batch_id}] status: {result.status}")
    if result.report:
        print(f"  gate: {'pass' if result.report.gate_passed else 'FAIL ' + '; '.join(result.report.gate_reasons)}")
        print(f"  rank: {result.report.rank_score}/10 ({result.report.scorer})")
    if result.verdict:
        print(f"  critic: {result.verdict.decision} — {'; '.join(result.verdict.reasons)}")
    if result.audio_path:
        print(f"  audio: {result.audio_path}")
    print(f"  manifest: {result.manifest_path}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="soundlabel",
                                description="run an AI music label from one box")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-w", "--workspace", default="./label",
                   help="label workspace directory (default ./label)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create a label workspace")

    roster = sub.add_parser("roster", help="manage the artist roster")
    rsub = roster.add_subparsers(dest="roster_cmd", required=True)
    radd = rsub.add_parser("add", help="sign an artist")
    radd.add_argument("slug")
    radd.add_argument("--name", default="")
    radd.add_argument("--language", default="en")
    radd.add_argument("--profile", default="", help="sonic profile, free text")
    rsub.add_parser("list", help="list the roster")

    produce = sub.add_parser("produce", help="run one production batch")
    produce.add_argument("artist")
    produce.add_argument("--backend", default="mock")
    produce.add_argument("--allow-paid", action="store_true",
                         help="permit a backend with a nonzero cost estimate")
    produce.add_argument("--llm", action="store_true",
                         help="use LLM-backed A&R and Critic agents (spends API "
                              "tokens; needs ANTHROPIC_API_KEY and soundlabel[llm])")

    sub.add_parser("catalog", help="list released tracks")

    demo = sub.add_parser("demo", help="end-to-end tour with a demo artist")
    demo.add_argument("--tracks", type=int, default=3)

    args = p.parse_args(argv)
    {"init": cmd_init, "roster": cmd_roster, "produce": cmd_produce,
     "catalog": cmd_catalog, "demo": cmd_demo}[args.cmd](args)


if __name__ == "__main__":
    main()
