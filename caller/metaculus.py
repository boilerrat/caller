"""Metaculus integration: list tournament questions, submit forecasts.

Phase 3. This module is deliberately a *thin* client over three Metaculus
API endpoints, ported from Metaculus's own no-framework reference bot
(github.com/Metaculus/metac-bot-template). Metaculus also ships a full
`forecasting-tools` framework, but adopting it would replace the caller
pipeline — its own research, reasoning, and aggregation — which is exactly
the part of this project we want to own and calibrate. So: their endpoints,
our pipeline.

The three endpoints:

    GET  /api/posts/                list posts, filterable by tournament,
                                    status, and forecast type
    POST /api/questions/forecast/   submit predictions (JSON *array*)
    POST /api/comments/create/      attach the rationale as a comment

Only binary questions are handled — that is what the reasoning pipeline
produces. Multiple-choice and numeric questions are filtered out server-side
via `forecast_type=binary`.

Metaculus questions arrive already formalized (title, resolution criteria,
fine print, close date), so the `--formalize` pass is unnecessary on this
path; the mapper below just reassembles those fields into our Question
contract.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

import requests

from .question import Question

API_BASE_URL = "https://www.metaculus.com/api"

# Tournament slugs/ids worth knowing (from the official bot template):
#   "bot-testing-area"  — sandbox for validating a bot end-to-end
#   "minibench"         — smaller live benchmark tournament
#   33022               — Summer 2026 AI Benchmarking tournament
DEFAULT_TOURNAMENT = "bot-testing-area"


@dataclass
class MetaculusQuestion:
    """The slice of a Metaculus post the pipeline needs."""

    post_id: int
    question_id: int
    title: str
    description: str = ""
    resolution_criteria: str = ""
    fine_print: str = ""
    scheduled_resolve_time: str = ""  # ISO timestamp from the API
    already_forecasted: bool = False

    @property
    def url(self) -> str:
        return f"https://www.metaculus.com/questions/{self.post_id}/"

    def to_question(self) -> Question:
        """Reassemble Metaculus's structured fields into our Question contract.

        The description gives background context, the resolution criteria and
        fine print pin down YES vs NO. Description is truncated defensively —
        some Metaculus backgrounds run to thousands of words, and the digest
        plus criteria matter more than exhaustive background in the prompt.
        """
        parts = []
        if self.resolution_criteria:
            parts.append(self.resolution_criteria.strip())
        if self.fine_print:
            parts.append(f"Fine print: {self.fine_print.strip()}")
        if self.description:
            desc = self.description.strip()
            if len(desc) > 1500:
                desc = desc[:1500] + " [...]"
            parts.append(f"Background: {desc}")
        return Question(
            text=self.title,
            resolution_date=_parse_iso_date(self.scheduled_resolve_time),
            criteria=" ".join(parts),
        )


def _parse_iso_date(timestamp: str) -> date:
    """API timestamps look like '2026-12-31T23:00:00Z' — keep just the date."""
    if not timestamp:
        return date.max  # unresolvable-by-date questions: keep pipeline moving
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date()


class MetaculusClient:
    def __init__(self, token: str):
        if not token:
            raise ValueError(
                "METACULUS_TOKEN is not set. Create a bot account and token "
                "at https://www.metaculus.com/aib/ and add it to .env."
            )
        self.headers = {"Authorization": f"Token {token}"}

    def _check(self, resp: requests.Response, context: str) -> None:
        # Same policy as the Tavily backend: surface the response body, since
        # that is where Metaculus puts the actual reason for a rejection.
        if not resp.ok:
            raise ValueError(
                f"Metaculus {context} failed (HTTP {resp.status_code}): "
                f"{resp.text.strip()[:500]}"
            )

    # --- reads ------------------------------------------------------------

    def list_open_binary(
        self, tournament: str | int, count: int = 50
    ) -> list[MetaculusQuestion]:
        """Open binary questions in a tournament, oldest-closing first."""
        resp = requests.get(
            f"{API_BASE_URL}/posts/",
            headers=self.headers,
            params={
                "tournaments": [tournament],
                "statuses": "open",
                "forecast_type": "binary",
                "limit": count,
                "order_by": "-hotness",
            },
            timeout=30,
        )
        self._check(resp, "question listing")

        questions = []
        for post in resp.json().get("results", []):
            q = post.get("question")
            if not q or q.get("type") != "binary":
                continue  # defensive: notebooks/groups can slip into results
            latest = (q.get("my_forecasts") or {}).get("latest") or {}
            questions.append(
                MetaculusQuestion(
                    post_id=post["id"],
                    question_id=q["id"],
                    title=q.get("title", post.get("title", "")),
                    description=q.get("description", ""),
                    resolution_criteria=q.get("resolution_criteria", ""),
                    fine_print=q.get("fine_print", ""),
                    scheduled_resolve_time=q.get("scheduled_resolve_time", ""),
                    already_forecasted=bool(latest.get("forecast_values")),
                )
            )
        return questions

    def already_forecasted(self, post_id: int) -> bool:
        """Whether this bot account has a standing forecast on the post.

        The /posts/ *list* response returns my_forecasts as null regardless
        of forecast state (verified live) — only the per-post detail endpoint
        populates it. So dedup requires one extra GET per candidate; the CLI
        checks lazily, only until its sweep limit is filled.
        """
        resp = requests.get(
            f"{API_BASE_URL}/posts/{post_id}/",
            headers=self.headers,
            timeout=30,
        )
        self._check(resp, f"detail fetch for post {post_id}")
        question = resp.json().get("question") or {}
        latest = (question.get("my_forecasts") or {}).get("latest") or {}
        return bool(latest.get("forecast_values"))

    # --- writes -----------------------------------------------------------

    def submit(self, question_id: int, probability: float) -> None:
        """Submit a binary forecast. The endpoint expects a JSON *array* —
        one entry per question — even for a single prediction."""
        resp = requests.post(
            f"{API_BASE_URL}/questions/forecast/",
            headers=self.headers,
            json=[
                {
                    "question": question_id,
                    "source": "api",
                    "probability_yes": probability,
                    "probability_yes_per_category": None,
                    "continuous_cdf": None,
                }
            ],
            timeout=30,
        )
        self._check(resp, f"forecast submission for question {question_id}")

    def post_comment(self, post_id: int, text: str) -> None:
        """Attach the rationale as a private comment on the post.

        Private keeps the bot's reasoning out of the public thread while
        still satisfying tournaments that expect submitted rationales."""
        resp = requests.post(
            f"{API_BASE_URL}/comments/create/",
            headers=self.headers,
            json={
                "text": text,
                "parent": None,
                "included_forecast": True,
                "is_private": True,
                "on_post": post_id,
            },
            timeout=30,
        )
        self._check(resp, f"comment on post {post_id}")
