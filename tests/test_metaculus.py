"""Tests for the Metaculus client, mapper, and CLI sweep.

Network calls are stubbed with canned JSON shaped like the real /posts/
response (per the official metac-bot-template). What is pinned down here:
response parsing, the already-forecasted skip signal, the exact submission
payload shape (a JSON array — an easy thing to silently get wrong), error
surfacing, the Question mapping, and the dry-run guarantee of no side
effects.
"""

from datetime import date

import pytest

from caller import metaculus
from caller.cli import main
from caller.metaculus import (
    API_BASE_URL,
    MetaculusClient,
    MetaculusQuestion,
    _parse_iso_date,
)


def _post(post_id=101, qid=201, forecasted=False, qtype="binary"):
    """A minimal /posts/ result entry shaped like the real API response."""
    return {
        "id": post_id,
        "title": f"post {post_id}",
        "question": {
            "id": qid,
            "type": qtype,
            "title": f"Will question {qid} resolve YES?",
            "description": "Some background.",
            "resolution_criteria": "Resolves YES if X.",
            "fine_print": "Edge cases resolve NO.",
            "scheduled_resolve_time": "2026-12-31T23:00:00Z",
            "my_forecasts": {
                "latest": {"forecast_values": [0.3, 0.7]} if forecasted else None
            },
        },
    }


class FakeResponse:
    def __init__(self, payload=None, ok=True, status_code=200, text=""):
        self._payload = payload or {}
        self.ok = ok
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


# --- client construction and error surfacing ---------------------------


def test_client_requires_token():
    with pytest.raises(ValueError, match="METACULUS_TOKEN"):
        MetaculusClient("")


def test_client_sets_token_header():
    client = MetaculusClient("fake-token")
    assert client.headers == {"Authorization": "Token fake-token"}


def test_error_surfaces_response_body(monkeypatch):
    monkeypatch.setattr(
        metaculus.requests, "get",
        lambda *a, **k: FakeResponse(ok=False, status_code=403,
                                     text='{"detail":"Invalid token."}'),
    )
    client = MetaculusClient("bad-token")
    with pytest.raises(ValueError, match="Invalid token"):
        client.list_open_binary("bot-testing-area")


# --- listing and parsing ------------------------------------------------


def test_list_open_binary_parses_posts(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["url"], captured["params"] = url, params
        return FakeResponse({"results": [_post(101, 201), _post(102, 202)]})

    monkeypatch.setattr(metaculus.requests, "get", fake_get)
    questions = MetaculusClient("t").list_open_binary("minibench")

    assert captured["url"] == f"{API_BASE_URL}/posts/"
    assert captured["params"]["tournaments"] == ["minibench"]
    assert captured["params"]["statuses"] == "open"
    assert captured["params"]["forecast_type"] == "binary"
    assert [q.question_id for q in questions] == [201, 202]
    assert questions[0].post_id == 101
    assert not questions[0].already_forecasted


def test_list_flags_already_forecasted(monkeypatch):
    monkeypatch.setattr(
        metaculus.requests, "get",
        lambda *a, **k: FakeResponse(
            {"results": [_post(101, 201, forecasted=True), _post(102, 202)]}
        ),
    )
    questions = MetaculusClient("t").list_open_binary("minibench")
    assert questions[0].already_forecasted
    assert not questions[1].already_forecasted


def test_list_filters_non_binary_defensively(monkeypatch):
    posts = [_post(101, 201), _post(102, 202, qtype="numeric"), {"id": 103}]
    monkeypatch.setattr(
        metaculus.requests, "get",
        lambda *a, **k: FakeResponse({"results": posts}),
    )
    questions = MetaculusClient("t").list_open_binary("minibench")
    assert [q.question_id for q in questions] == [201]


def test_already_forecasted_reads_post_detail(monkeypatch):
    """The list response carries my_forecasts as null (verified live), so
    forecast-state dedup must hit the per-post detail endpoint."""
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["url"] = url
        return FakeResponse({
            "id": 101,
            "question": {
                "id": 201,
                "my_forecasts": {"latest": {"forecast_values": [0.99, 0.01]}},
            },
        })

    monkeypatch.setattr(metaculus.requests, "get", fake_get)
    assert MetaculusClient("t").already_forecasted(101) is True
    assert captured["url"] == f"{API_BASE_URL}/posts/101/"


def test_already_forecasted_false_when_no_forecast(monkeypatch):
    monkeypatch.setattr(
        metaculus.requests, "get",
        lambda *a, **k: FakeResponse(
            {"id": 101, "question": {"id": 201, "my_forecasts": None}}
        ),
    )
    assert MetaculusClient("t").already_forecasted(101) is False


# --- submission payloads ------------------------------------------------


def test_submit_sends_array_payload(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"], captured["json"] = url, json
        return FakeResponse()

    monkeypatch.setattr(metaculus.requests, "post", fake_post)
    MetaculusClient("t").submit(201, 0.42)

    assert captured["url"] == f"{API_BASE_URL}/questions/forecast/"
    # The endpoint expects a JSON *array*, one entry per question.
    assert isinstance(captured["json"], list)
    entry = captured["json"][0]
    assert entry["question"] == 201
    assert entry["probability_yes"] == 0.42
    assert entry["probability_yes_per_category"] is None
    assert entry["continuous_cdf"] is None


def test_post_comment_is_private_on_post(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"], captured["json"] = url, json
        return FakeResponse()

    monkeypatch.setattr(metaculus.requests, "post", fake_post)
    MetaculusClient("t").post_comment(101, "rationale text")

    assert captured["url"] == f"{API_BASE_URL}/comments/create/"
    assert captured["json"]["on_post"] == 101
    assert captured["json"]["text"] == "rationale text"
    assert captured["json"]["is_private"] is True


# --- Question mapping ---------------------------------------------------


def test_to_question_assembles_criteria():
    mq = MetaculusQuestion(
        post_id=101, question_id=201, title="Will X happen?",
        description="Background info.",
        resolution_criteria="Resolves YES if X.",
        fine_print="Edge cases resolve NO.",
        scheduled_resolve_time="2026-12-31T23:00:00Z",
    )
    q = mq.to_question()
    assert q.text == "Will X happen?"
    assert q.resolution_date == date(2026, 12, 31)
    assert "Resolves YES if X." in q.criteria
    assert "Fine print: Edge cases resolve NO." in q.criteria
    assert "Background: Background info." in q.criteria


def test_to_question_truncates_long_description():
    mq = MetaculusQuestion(
        post_id=1, question_id=2, title="t", description="x" * 5000,
        scheduled_resolve_time="2026-12-31T00:00:00Z",
    )
    assert "[...]" in mq.to_question().criteria
    assert len(mq.to_question().criteria) < 2000


def test_parse_iso_date_handles_missing():
    assert _parse_iso_date("") == date.max
    assert _parse_iso_date("2027-03-01T12:00:00Z") == date(2027, 3, 1)


def test_url_derived_from_post_id():
    mq = MetaculusQuestion(post_id=38000, question_id=1, title="t")
    assert mq.url == "https://www.metaculus.com/questions/38000/"


# --- CLI sweep (mock reasoning, stubbed client) --------------------------


class StubClient:
    def __init__(self, questions):
        self.questions = questions
        self.submitted = []
        self.comments = []

    def list_open_binary(self, tournament, count=50):
        return self.questions

    def already_forecasted(self, post_id):
        return any(
            q.post_id == post_id and q.already_forecasted
            for q in self.questions
        )

    def submit(self, question_id, probability):
        self.submitted.append((question_id, probability))

    def post_comment(self, post_id, text):
        self.comments.append(post_id)


def _stub_questions(n=2, forecasted=0):
    qs = []
    for i in range(n):
        qs.append(MetaculusQuestion(
            post_id=100 + i, question_id=200 + i,
            title=f"Will question {i} resolve YES?",
            resolution_criteria="Resolves YES if X.",
            scheduled_resolve_time="2026-12-31T00:00:00Z",
            already_forecasted=(i < forecasted),
        ))
    return qs


@pytest.fixture()
def cli_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLER_DB", str(tmp_path / "cli.db"))
    monkeypatch.setenv("METACULUS_TOKEN", "fake")


def test_metaculus_mock_sweep_is_forced_dry_run(cli_db, monkeypatch, capsys):
    stub = StubClient(_stub_questions(2))
    monkeypatch.setattr("caller.cli._make_client", lambda cfg: stub)

    main(["metaculus", "--mock", "--runs", "2"])
    out = capsys.readouterr().out

    assert "--mock implies --dry-run" in out
    assert "DRY RUN" in out
    assert stub.submitted == []      # nothing reached the API
    assert stub.comments == []
    main(["log"])
    assert "Ledger is empty" in capsys.readouterr().out  # nothing recorded


def test_metaculus_sweep_skips_already_forecasted(cli_db, monkeypatch, capsys):
    stub = StubClient(_stub_questions(3, forecasted=1))
    monkeypatch.setattr("caller.cli._make_client", lambda cfg: stub)

    main(["metaculus", "--mock", "--runs", "2"])
    out = capsys.readouterr().out
    assert "Skipping 1 question(s) already forecasted" in out
    assert "2/2 question(s) processed (dry run)" in out


def test_metaculus_sweep_respects_limit(cli_db, monkeypatch, capsys):
    stub = StubClient(_stub_questions(4))
    monkeypatch.setattr("caller.cli._make_client", lambda cfg: stub)

    main(["metaculus", "--mock", "--runs", "2", "--limit", "1"])
    out = capsys.readouterr().out
    assert "Limiting to 1 of 4" in out
    assert "1/1 question(s) processed" in out


def test_metaculus_live_sweep_submits_and_records(cli_db, monkeypatch, capsys):
    """Non-dry-run path: submits, comments, and records with metaculus ids.

    Reasoning stays mocked via monkeypatch (not --mock, which forces dry-run).
    """
    from caller import reasoning

    stub = StubClient(_stub_questions(1))
    monkeypatch.setattr("caller.cli._make_client", lambda cfg: stub)
    monkeypatch.setattr(
        "caller.cli.reasoning.run_once",
        lambda q, d, cfg: reasoning.run_once_mock(q, d),
    )
    monkeypatch.setattr(
        "caller.cli.research.gather",
        lambda q, cfg, mock=False: "stub digest",
    )

    main(["metaculus", "--runs", "3"])
    out = capsys.readouterr().out

    assert len(stub.submitted) == 1
    qid, prob = stub.submitted[0]
    assert qid == 200
    assert 0.0 < prob < 1.0
    assert stub.comments == [100]
    assert "logged as prediction #1" in out

    # The ledger row carries the Metaculus linkage.
    import sqlite3, os
    conn = sqlite3.connect(os.environ["CALLER_DB"])
    row = conn.execute(
        "SELECT metaculus_qid, metaculus_url FROM predictions WHERE id = 1"
    ).fetchone()
    assert row[0] == 200
    assert row[1] == "https://www.metaculus.com/questions/100/"


def test_metaculus_sweep_continues_after_one_failure(cli_db, monkeypatch, capsys):
    stub = StubClient(_stub_questions(2))
    monkeypatch.setattr("caller.cli._make_client", lambda cfg: stub)

    real_gather = None
    call_count = {"n": 0}

    def flaky_gather(q, cfg, mock=False):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError("simulated research failure")
        return "stub digest"

    monkeypatch.setattr("caller.cli.research.gather", flaky_gather)

    main(["metaculus", "--mock", "--runs", "2"])
    captured = capsys.readouterr()
    assert "FAILED on" in captured.err
    assert "1/2 question(s) processed" in captured.out


def test_metaculus_migration_adds_columns_to_phase2_db(tmp_path):
    """A Phase-2-era ledger (has raw_question, lacks metaculus columns)
    must migrate cleanly."""
    import sqlite3
    from caller.ledger import Ledger

    db = str(tmp_path / "phase2.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE predictions (
               id              INTEGER PRIMARY KEY AUTOINCREMENT,
               question        TEXT NOT NULL,
               raw_question    TEXT,
               criteria        TEXT,
               resolution_date TEXT NOT NULL,
               probability     REAL NOT NULL,
               runs            INTEGER NOT NULL,
               spread          REAL NOT NULL,
               rationale       TEXT,
               created_at      TEXT NOT NULL,
               outcome         INTEGER,
               brier           REAL
           )"""
    )
    conn.commit()
    conn.close()

    book = Ledger(db)
    cols = {r[1] for r in book.conn.execute("PRAGMA table_info(predictions)")}
    assert {"metaculus_qid", "metaculus_url"} <= cols
