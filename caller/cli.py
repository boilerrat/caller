"""caller CLI — forecast, resolve, log, score.

Usage examples:

    # Full pipeline offline (no API keys needed) to verify the install:
    python -m caller forecast "Will it rain in Toronto tomorrow?" \
        --date 2026-07-07 --mock

    # A real forecast, 5 independent runs, aggregated by median:
    python -m caller forecast \
        "Will the Bank of Canada policy rate be below 2.00% on Dec 31 2026?" \
        --date 2026-12-31 --runs 5

    # When reality answers, log it — this is what builds the track record:
    python -m caller resolve 1 --outcome no

    # Review the ledger and calibration:
    python -m caller log
    python -m caller score
"""

import argparse
import sys
from datetime import date

from . import aggregate, config, formalize, ledger, metaculus, reasoning, research
from .question import Question


def _formalize_interactively(args, cfg) -> formalize.Formalization | None:
    """Run the formalization pass with a human approval gate.

    Returns the accepted Formalization, or None if the user aborts. The loop
    lets the user re-roll the proposal — formalization is judgment work, and
    a second draft is sometimes sharper than arguing with the first.
    """
    resolution_date = date.fromisoformat(args.date)
    while True:
        print("Formalizing question...")
        if args.mock:
            proposal = formalize.propose_mock(args.question, resolution_date)
        else:
            proposal = formalize.propose(args.question, resolution_date, cfg)
        print("\n" + proposal.display() + "\n")
        choice = input("[a]ccept / [r]etry / [q]uit: ").strip().lower()
        if choice in ("a", "accept", "y", "yes"):
            return proposal
        if choice in ("q", "quit", "n", "no"):
            return None
        # anything else (including "r") re-rolls the proposal


def _apply_formalization(
    proposal: formalize.Formalization, resolution_date: date
) -> Question:
    """Build the Question that actually gets forecast from an accepted proposal."""
    # The named resolution source is part of what makes the criteria
    # checkable, so it travels with them into the prompt and the ledger.
    criteria = (
        f"{proposal.criteria} Check against: {proposal.resolution_source}"
        if proposal.resolution_source
        else proposal.criteria
    )
    return Question(
        text=proposal.question,
        resolution_date=resolution_date,
        criteria=criteria,
    )


def cmd_forecast(args, cfg) -> None:
    raw_question = None  # set when a formalization pass rewrites the question
    q = Question(
        text=args.question,
        resolution_date=date.fromisoformat(args.date),
        criteria=args.criteria or "",
    )

    if args.formalize:
        proposal = _formalize_interactively(args, cfg)
        if proposal is None:
            print("Aborted — no forecast made.")
            return
        raw_question = args.question
        q = _apply_formalization(proposal, q.resolution_date)

    print(f"Researching: {q.text}")
    digest = research.gather(q, cfg, mock=args.mock)
    print(f"Research digest assembled ({len(digest)} chars).\n")

    runs = []
    n = args.runs or cfg.default_runs
    for i in range(n):
        print(f"Reasoning run {i + 1}/{n}...", end=" ", flush=True)
        if args.mock:
            run = reasoning.run_once_mock(q, digest)
        else:
            run = reasoning.run_once(q, digest, cfg)
        print(f"p = {run.probability:.2f}")
        runs.append(run)

    forecast = aggregate.aggregate(runs)
    print("\n" + forecast.summary())

    book = ledger.Ledger(cfg.db_path)
    pid = book.record(q, forecast, raw_question=raw_question)
    print(f"\nLogged as prediction #{pid} in {cfg.db_path}")

    # Show the representative rationale so the human can sanity-check it.
    rep = min(runs, key=lambda r: abs(r.probability - forecast.probability))
    print(f"\nRepresentative rationale:\n{rep.rationale}")


def _forecast_one_metaculus(mq, args, cfg, book, client) -> None:
    """Run the full pipeline on one Metaculus question; submit unless dry-run."""
    q = mq.to_question()
    print(f"\n=== {mq.title}\n    {mq.url}")

    digest = research.gather(q, cfg, mock=args.mock)
    print(f"Research digest assembled ({len(digest)} chars).")

    runs = []
    n = args.runs or cfg.default_runs
    for i in range(n):
        print(f"Reasoning run {i + 1}/{n}...", end=" ", flush=True)
        if args.mock:
            run = reasoning.run_once_mock(q, digest)
        else:
            run = reasoning.run_once(q, digest, cfg)
        print(f"p = {run.probability:.2f}")
        runs.append(run)

    forecast = aggregate.aggregate(runs)
    print(forecast.summary())
    rep = min(runs, key=lambda r: abs(r.probability - forecast.probability))

    if args.dry_run:
        # No side effects at all: nothing submitted, nothing recorded. The
        # point of a dry run is to inspect what *would* happen — recording
        # test sweeps would pollute the calibration ledger.
        print("DRY RUN — not submitted, not recorded.")
        print(f"Would submit p = {forecast.probability:.2f} "
              f"for question {mq.question_id}.")
        print(f"Rationale:\n{rep.rationale}")
        return

    client.submit(mq.question_id, forecast.probability)
    comment = (
        f"caller bot — median of {len(runs)} independent runs "
        f"(spread {forecast.spread:.2f}).\n\n{rep.rationale}"
    )
    client.post_comment(mq.post_id, comment)
    pid = book.record(
        q,
        forecast,
        metaculus_qid=mq.question_id,
        metaculus_url=mq.url,
    )
    print(f"Submitted p = {forecast.probability:.2f} and rationale comment; "
          f"logged as prediction #{pid}.")


def _make_client(cfg) -> metaculus.MetaculusClient:
    """Client factory — a seam so tests can stub the network side."""
    return metaculus.MetaculusClient(cfg.metaculus_token)


def cmd_metaculus(args, cfg) -> None:
    # Mock reasoning must never reach a live tournament — a --mock sweep is
    # for exercising plumbing, so it is forced into dry-run mode.
    if args.mock and not args.dry_run:
        print("--mock implies --dry-run (mock forecasts are never submitted).")
        args.dry_run = True

    client = _make_client(cfg)
    questions = client.list_open_binary(args.tournament)

    # The list response doesn't reliably carry forecast state, so each
    # candidate needs a detail check — done lazily, stopping once the sweep
    # limit is filled so a big tournament doesn't mean dozens of extra GETs.
    limit = args.limit or len(questions)
    fresh, skipped = [], 0
    for mq in questions:
        if len(fresh) >= limit:
            break
        if mq.already_forecasted or client.already_forecasted(mq.post_id):
            skipped += 1
            continue
        fresh.append(mq)

    if skipped:
        print(f"Skipping {skipped} question(s) already forecasted.")
    if not fresh:
        print(f"No open unforecasted binary questions in '{args.tournament}'.")
        return
    if len(questions) > len(fresh) + skipped:
        print(f"Limiting to {len(fresh)} of {len(questions)} open questions.")

    book = ledger.Ledger(cfg.db_path)
    failures = []
    for mq in fresh:
        try:
            _forecast_one_metaculus(mq, args, cfg, book, client)
        except (ValueError, KeyError) as exc:
            # One bad question (thin research, malformed model output, a
            # rejected submission) must not abort the rest of the sweep.
            print(f"FAILED on '{mq.title}': {exc}", file=sys.stderr)
            failures.append(mq.title)

    print(f"\nDone: {len(fresh) - len(failures)}/{len(fresh)} question(s) "
          f"processed{' (dry run)' if args.dry_run else ''}.")
    if failures:
        print("Failed: " + "; ".join(failures), file=sys.stderr)


def cmd_formalize(args, cfg) -> None:
    """Preview a formalization without forecasting — useful for sharpening a
    question (or checking whether it needs sharpening) before spending the
    research and reasoning calls on it."""
    resolution_date = date.fromisoformat(args.date)
    if args.mock:
        proposal = formalize.propose_mock(args.question, resolution_date)
    else:
        proposal = formalize.propose(args.question, resolution_date, cfg)
    print(proposal.display())


def cmd_resolve(args, cfg) -> None:
    book = ledger.Ledger(cfg.db_path)
    outcome_yes = args.outcome.lower() in ("yes", "y", "1", "true")
    brier = book.resolve(args.id, outcome_yes)
    print(
        f"Prediction #{args.id} resolved "
        f"{'YES' if outcome_yes else 'NO'} — Brier score: {brier:.4f}"
    )


def cmd_log(args, cfg) -> None:
    book = ledger.Ledger(cfg.db_path)
    rows = book.rows()
    if not rows:
        print("Ledger is empty.")
        return
    for r in rows:
        status = (
            f"resolved {'YES' if r.outcome else 'NO'} (brier {r.brier:.4f})"
            if r.brier is not None
            else "open"
        )
        print(f"#{r.id}  p={r.probability:.2f}  due {r.resolution_date}  "
              f"[{status}]  {r.question}")


def cmd_score(args, cfg) -> None:
    book = ledger.Ledger(cfg.db_path)
    print(book.calibration_summary())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="caller", description="AI superforecasting bot (proof of concept)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_fc = sub.add_parser("forecast", help="research, reason, aggregate, log")
    p_fc.add_argument("question", help="the forecasting question (binary YES/NO)")
    p_fc.add_argument("--date", required=True, help="resolution date, YYYY-MM-DD")
    p_fc.add_argument("--criteria", help="explicit resolution criteria")
    p_fc.add_argument("--runs", type=int, help="number of reasoning runs to aggregate")
    p_fc.add_argument("--mock", action="store_true",
                      help="run offline with canned research and reasoning")
    p_fc.add_argument("--formalize", action="store_true",
                      help="propose crisp resolution criteria for approval "
                           "before researching")
    p_fc.set_defaults(func=cmd_forecast)

    p_fm = sub.add_parser("formalize",
                          help="preview a question formalization without forecasting")
    p_fm.add_argument("question", help="the raw forecasting question")
    p_fm.add_argument("--date", required=True, help="resolution date, YYYY-MM-DD")
    p_fm.add_argument("--mock", action="store_true",
                      help="run offline with a canned formalization")
    p_fm.set_defaults(func=cmd_formalize)

    p_mc = sub.add_parser(
        "metaculus",
        help="forecast open tournament questions and submit to Metaculus",
    )
    p_mc.add_argument(
        "--tournament", default=metaculus.DEFAULT_TOURNAMENT,
        help="tournament slug or id (default: %(default)s — the sandbox; "
             "try 'minibench' or 33022 for live tournaments)")
    p_mc.add_argument(
        "--limit", type=int, default=5,
        help="max questions to forecast this sweep (default: %(default)s; "
             "each costs ~7 LLM calls + search)")
    p_mc.add_argument("--runs", type=int,
                      help="number of reasoning runs to aggregate")
    p_mc.add_argument("--dry-run", action="store_true",
                      help="run the pipeline but submit and record nothing")
    p_mc.add_argument("--mock", action="store_true",
                      help="offline research/reasoning (implies --dry-run)")
    p_mc.set_defaults(func=cmd_metaculus)

    p_res = sub.add_parser("resolve", help="record a question's real-world outcome")
    p_res.add_argument("id", type=int, help="prediction id from the ledger")
    p_res.add_argument("--outcome", required=True, choices=["yes", "no"],
                       help="how the question actually resolved")
    p_res.set_defaults(func=cmd_resolve)

    p_log = sub.add_parser("log", help="list all predictions in the ledger")
    p_log.set_defaults(func=cmd_log)

    p_score = sub.add_parser("score", help="show calibration summary (mean Brier)")
    p_score.set_defaults(func=cmd_score)

    args = parser.parse_args(argv)
    cfg = config.load()
    try:
        args.func(args, cfg)
    except (ValueError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
