"""Transparent, rule-based fit score (0-100) for "how good a customer is this contractor for a
roofing distributor". Every point is explained in `breakdown` so reps can see *why* and argue with it.

The weights are a first guess - the intent is to replace them with a model trained on the
distributor's own CRM outcomes (won / lost / spend) once that data is available.
"""

import math
from dataclasses import dataclass

from app.schemas import ContractorRecord, ScoreFactor

TIER_POINTS = {"master elite": 35, "certified plus": 25, "certified": 12}


@dataclass
class ScoreResult:
    score: int
    priority: str
    breakdown: list[ScoreFactor]


def score_contractor(c: ContractorRecord) -> ScoreResult:
    factors: list[ScoreFactor] = []

    tier_pts = TIER_POINTS.get((c.cert_tier or "").lower(), 0)
    factors.append(
        ScoreFactor(
            factor="GAF certification",
            points=tier_pts,
            max_points=35,
            note=c.cert_tier or "No certification listed",
        )
    )

    # Review count is our best public proxy for job volume (= shingle purchases). Log scale so 400
    # reviews isn't 10x better than 40.
    reviews = c.review_count or 0
    vol_pts = min(25.0, 25 * math.log10(1 + reviews) / math.log10(1 + 400))
    factors.append(
        ScoreFactor(
            factor="Job volume (reviews)",
            points=round(vol_pts, 1),
            max_points=25,
            note=f"{reviews} reviews",
        )
    )

    rating_pts = 0.0 if c.rating is None else max(0.0, min(15.0, (c.rating - 3.5) / 1.5 * 15))
    factors.append(
        ScoreFactor(
            factor="Reputation",
            points=round(rating_pts, 1),
            max_points=15,
            note="No rating yet" if c.rating is None else f"{c.rating:.1f} / 5",
        )
    )

    # Distributors deliver from a branch; closer = cheaper to serve.
    dist = c.distance_mi
    prox_pts = 0.0 if dist is None else max(0.0, 15 * (1 - dist / 25))
    factors.append(
        ScoreFactor(
            factor="Proximity",
            points=round(prox_pts, 1),
            max_points=15,
            note="Unknown" if dist is None else f"{dist:.1f} mi from search zip",
        )
    )

    years = c.years_in_business or 0
    ten_pts = min(10.0, years / 20 * 10)
    factors.append(
        ScoreFactor(
            factor="Tenure",
            points=round(ten_pts, 1),
            max_points=10,
            note=f"{years} yrs" if years else "Unknown",
        )
    )

    total = round(sum(f.points for f in factors))
    priority = "hot" if total >= 80 else "warm" if total >= 55 else "cold"
    return ScoreResult(score=total, priority=priority, breakdown=factors)
