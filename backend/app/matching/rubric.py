from functools import lru_cache
from importlib import resources

from pydantic import BaseModel, model_validator


class CriterionGuide(BaseModel):
    high: str
    mid: str
    low: str


class Criterion(BaseModel):
    id: str
    name: str
    weight: float
    measures: str
    guide: CriterionGuide


class Rubric(BaseModel):
    manufacturer: str
    display_name: str
    version: str
    status: str
    context: str
    criteria: list[Criterion]

    @model_validator(mode="after")
    def _weights_positive(self) -> "Rubric":
        if not self.criteria or any(c.weight <= 0 for c in self.criteria):
            raise ValueError("rubric needs at least one criterion and every weight must be > 0")
        if len({c.id for c in self.criteria}) != len(self.criteria):
            raise ValueError("duplicate criterion id")
        return self


@lru_cache
def load_rubric(manufacturer: str) -> Rubric:
    path = resources.files("app.matching").joinpath(f"rubrics/{manufacturer}.json")
    if not path.is_file():
        raise ValueError(f"no rubric for manufacturer {manufacturer!r}")
    return Rubric.model_validate_json(path.read_text())
