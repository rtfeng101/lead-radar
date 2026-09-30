"""Turns a contractor + its score into a rep-ready account-planning brief.

Two interchangeable generators behind one interface:
  * HeuristicInsights - deterministic templates. Free, offline, the default.
  * ClaudeInsights    - LLM-written brief with a structured-output schema.
"""

import json
import logging
from typing import Protocol

import anthropic

from app.config import settings
from app.pipeline.scoring import ScoreResult
from app.schemas import ContractorRecord, Insights

log = logging.getLogger(__name__)


class InsightGenerator(Protocol):
    name: str

    def generate(self, c: ContractorRecord, s: ScoreResult) -> Insights: ...


class HeuristicInsights:
    name = "heuristic"

    def generate(self, c: ContractorRecord, s: ScoreResult) -> Insights:
        tier = c.cert_tier or "uncertified"
        loc = ", ".join(p for p in (c.city, c.state) if p)
        summary = (
            f"{c.name} is a {tier} GAF contractor in {loc}"
            f"{f', {c.distance_mi:.1f} mi away' if c.distance_mi is not None else ''}. "
            f"Fit score {s.score}/100 ({s.priority})."
        )

        points: list[str] = []
        if c.cert_tier and c.cert_tier.lower() in ("master elite", "certified plus"):
            points.append(f"{c.cert_tier} status means access to GAF warranties they'll want to keep selling - lead with GAF product availability.")
        if (c.review_count or 0) >= 100:
            points.append(f"{c.review_count} reviews suggests steady volume; ask about their monthly square count.")
        if c.services:
            points.append("Works in " + ", ".join(c.services[:3]) + " - tailor the product conversation to those lines.")
        if not points:
            points.append("Thin public profile; open with discovery questions about their current supplier.")

        risks: list[str] = []
        if not c.website:
            risks.append("No website listed - harder to research; verify contact details.")
        if (c.review_count or 0) < 20:
            risks.append("Few reviews - volume and stability unproven.")
        if c.rating is not None and c.rating < 4.0:
            risks.append(f"Rating of {c.rating:.1f} may indicate quality or payment issues; check credit terms.")
        risks.append("Current supplier and purchase volume are unknown - confirm before pricing.")

        action = {
            "hot": "Call this week: offer a branch visit and pro-account setup.",
            "warm": "Send an intro email with account-opening details, follow up in a week.",
            "cold": "Add to a nurture list; revisit if their volume or tier changes.",
        }[s.priority]
        return Insights(summary=summary, talking_points=points, risks=risks, next_best_action=action)


SYSTEM_PROMPT = """\
You are a sales-intelligence analyst for a roofing-materials distributor. You will receive a \
GAF-listed roofing contractor and a rule-based fit score. Write a short brief for the account \
manager who is about to plan outreach.

Rules:
- Use only the facts provided. Never invent crew size, revenue, suppliers, owners or contact names.
- Anything valuable you cannot know from the data (current supplier, purchase volume, decision-maker) \
belongs under risks/unknowns, phrased as something to verify.
- Be specific and concise: summary of at most 2 sentences, 3-4 talking points, 1-3 risks, and one \
concrete next best action."""


class ClaudeInsights:
    def __init__(self) -> None:
        self.model = settings.insights_model
        self.name = self.model
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._fallback = HeuristicInsights()

    def generate(self, c: ContractorRecord, s: ScoreResult) -> Insights:
        payload = {
            "contractor": c.model_dump(exclude={"raw"}),
            "score": s.score,
            "priority": s.priority,
            "score_breakdown": [f.model_dump() for f in s.breakdown],
        }
        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=4000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(payload, sort_keys=True)}],
                output_format=Insights,
            )
        except (anthropic.RateLimitError, anthropic.APIConnectionError):
            raise  # transient: let the job queue retry with backoff
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise
            log.warning("insights: non-retryable API error %s for %s; using heuristic", e.status_code, c.source_id)
            self.name = "heuristic-fallback"
            return self._fallback.generate(c, s)

        if response.parsed_output is None:  # refusal / truncated output
            log.warning("insights: no parsed output (stop_reason=%s) for %s", response.stop_reason, c.source_id)
            self.name = "heuristic-fallback"
            return self._fallback.generate(c, s)
        self.name = self.model
        return response.parsed_output


def get_generator() -> InsightGenerator:
    return ClaudeInsights() if settings.insights_backend == "claude" else HeuristicInsights()
