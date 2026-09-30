import json

from app.matching.rubric import load_rubric
from app.matching.types import CriterionJudgement, Judgement, ReviewBatch, ReviewFacts


class FakeLLM:
    """Deterministic stand-in for OpenAILLM. Scores by average review rating so tests can assert ordering,
    and counts calls per schema so tests can assert what was (not) re-run."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.judge_inputs: list[dict] = []

    def count(self, schema_name: str) -> int:
        return self.calls.count(schema_name)

    def parse(self, *, model, system, user, schema):
        self.calls.append(schema.__name__)
        if schema is ReviewBatch:
            return ReviewBatch(items=[
                ReviewFacts(
                    id=r["id"],
                    sentiment="positive" if (r["rating"] or 3) >= 4 else "negative",
                    project="residential_replacement",
                    complaints=[] if (r["rating"] or 3) >= 4 else ["leak_or_defect"],
                    praises=["workmanship"] if (r["rating"] or 3) >= 4 else [],
                    brands=["Timberline"] if "GAF" in r["text"] else [],
                )
                for r in json.loads(user)
            ])
        if schema is Judgement:
            pack = json.loads(user)
            self.judge_inputs.append(pack)
            avg = pack["reviews"].get("avg_rating")
            cites = pack["reviews"].get("all_ids", [])[:1] or ["LISTING"]
            return Judgement(
                criteria=[
                    CriterionJudgement(
                        id=c.id,
                        score=None if avg is None else round(avg / 5 * 100),
                        confidence=0.8,
                        rationale=f"fake rationale for {c.id}",
                        evidence_ids=cites,
                    )
                    for c in load_rubric("gaf").criteria
                ],
                summary="fake summary",
                gaps=["fake gap"],
                next_best_action="fake next action",
            )
        raise AssertionError(f"unexpected schema {schema}")


import html as _html
import json as _json


def gaf_profile_html(
    *, legal_name="Acme Roofing Group LLC", street="1 Main St Suite 2", city="Springfield", state="NY", zip_code="10001",
    phone="(212) 555-0100", since=2015, employees="6-10", reviews=None, description=None,
    place_id="ChIJexamplePlaceId123",
) -> str:
    """Synthetic page with the same embedded structures as a real gaf.com profile (JSON-LD, details, data-reviews)."""
    if reviews is None:
        reviews = [
            {"Text": "Great re-roof, GAF Timberline shingles, crew was tidy.", "Rating": 5.0, "AuthorName": "Pat Example",
             "PublishTime": "2026-06-09T15:14:18.534015Z", "ReviewSource": "Google"},
            {"Text": "Leak came back after a month and they stopped answering.", "Rating": 2.0, "AuthorName": "Sam Sample",
             "PublishTime": "2026-04-15T23:58:06.789Z", "ReviewSource": "Google"},
        ]
    ld = {"@context": "https://schema.org", "@type": "LocalBusiness", "name": legal_name, "telephone": phone,
          "address": {"@type": "PostalAddress", "addressLocality": city, "addressRegion": state,
                      "Postalcode": zip_code, "streetAddress": street}}
    if description is not None:
        ld["description"] = description
    ld_json = _json.dumps(ld).replace("\\n", "\n")  # real pages carry RAW line breaks inside strings (invalid strict JSON)
    return f"""<html><head><script type="application/ld+json"> {ld_json} </script>
<script type="application/ld+json">{{"@type": "BreadcrumbList"}}</script></head><body>
<section class="contractor-details"><div class="contractor-details__info">
 <h3 class="contractor-details__title">Years in Business</h3>
 <p class="contractor-details__description">In business since {since}</p></div>
<div class="contractor-details__info"><h3 class="contractor-details__title">Number of Employees</h3>
 <p class="contractor-details__description">{employees}</p></div></section>
<section class="contractor-reviews" data-reviews="{_html.escape(_json.dumps(reviews), quote=True)}" data-module="contractor-reviews">
{'<a class="btn btn--primary contractor-reviews__cta" href="https://search.google.com/local/reviews?placeid=' + place_id + '">See All Reviews</a>' if place_id else ""}
</section></body></html>"""
