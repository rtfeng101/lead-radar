"""Live review collection - NOT IMPLEMENTED (deliberately not wired into get_collectors()).

Reviews are the richest signal for the LLM, but the source question is a product/legal decision:
  * Google Places API: legitimate and structured, but returns only ~5 reviews per place and its terms
    restrict storing review text beyond short-term caching.
  * Yelp Fusion: 3 short excerpts per business.
  * Review-data vendors (SerpApi, Outscraper, Birdeye...): full review history; paid, ToS varies.
  * LLM web search (e.g. Perplexity Sonar): returns a synthesized answer, not verbatim reviews. Useful for
    leads on *where* reviews live, but it is not evidence we should cite as a review.

Whichever is chosen implements `Collector` and emits `EvidenceItem(kind="review", payload={"rating": 1-5,
"text": str, "date": iso, "platform": str, "author": optional})` with a stable `external_id` (the
platform's review id). Nothing else in the pipeline needs to change: extraction and scoring pick it up.
"""
