"""End-to-end CLI tests in mock mode.

These run the real command paths — forecast, log, resolve, score, formalize —
against a temporary database, exercising the same wiring a user does. Mock
mode means no API keys and no network.
"""

import pytest

from caller.cli import main


@pytest.fixture()
def cli_db(tmp_path, monkeypatch):
    """Point the CLI at a throwaway database."""
    db = str(tmp_path / "cli.db")
    monkeypatch.setenv("CALLER_DB", db)
    return db


def test_full_mock_lifecycle(cli_db, capsys):
    """forecast → log → resolve → score, the whole Phase 1 loop."""
    main(["forecast", "Will X happen?", "--date", "2026-12-31",
          "--mock", "--runs", "3"])
    out = capsys.readouterr().out
    assert "Median probability:" in out
    assert "Logged as prediction #1" in out

    main(["log"])
    assert "#1" in capsys.readouterr().out

    main(["resolve", "1", "--outcome", "no"])
    assert "Brier score:" in capsys.readouterr().out

    main(["score"])
    out = capsys.readouterr().out
    assert "Resolved:          1" in out
    assert "Mean Brier score:" in out


def test_log_with_empty_ledger(cli_db, capsys):
    main(["log"])
    assert "Ledger is empty" in capsys.readouterr().out


def test_resolve_unknown_id_exits_nonzero(cli_db, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["resolve", "99", "--outcome", "yes"])
    assert exc.value.code == 1
    assert "Error:" in capsys.readouterr().err


def test_forecast_with_formalize_accept(cli_db, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "a")
    main(["forecast", "will x happen soon??", "--date", "2026-12-31",
          "--mock", "--formalize", "--runs", "2"])
    out = capsys.readouterr().out
    assert "Proposed formalization:" in out
    # The forecast must run against the formalized text, not the raw input.
    assert "Researching: [MOCK formalized]" in out
    assert "Logged as prediction #1" in out


def test_forecast_with_formalize_quit_logs_nothing(cli_db, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "q")
    main(["forecast", "will x happen soon??", "--date", "2026-12-31",
          "--mock", "--formalize"])
    assert "Aborted" in capsys.readouterr().out
    main(["log"])
    assert "Ledger is empty" in capsys.readouterr().out


def test_forecast_with_formalize_retry_then_accept(cli_db, monkeypatch, capsys):
    answers = iter(["r", "a"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    main(["forecast", "will x happen soon??", "--date", "2026-12-31",
          "--mock", "--formalize", "--runs", "2"])
    out = capsys.readouterr().out
    # The proposal was shown twice: once rejected, once accepted.
    assert out.count("Proposed formalization:") == 2
    assert "Logged as prediction #1" in out


def test_standalone_formalize_command(cli_db, capsys):
    main(["formalize", "will x happen soon??", "--date", "2026-12-31", "--mock"])
    out = capsys.readouterr().out
    assert "QUESTION:" in out
    # Preview only — nothing goes in the ledger.
    main(["log"])
    assert "Ledger is empty" in capsys.readouterr().out


def test_invalid_date_exits_nonzero(cli_db, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["forecast", "Will X happen?", "--date", "not-a-date", "--mock"])
    assert exc.value.code == 1
    assert "Error:" in capsys.readouterr().err
