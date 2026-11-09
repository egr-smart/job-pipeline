import json
from pathlib import Path

import pytest

from job_pipeline.config import Config
from job_pipeline.models import RawJob

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(rel: str):
    return json.loads((FIXTURES / rel).read_text())


def make_job(**kw) -> RawJob:
    defaults = dict(
        source="greenhouse", source_id="1", url="https://example.com/1",
        company="Acme", title="Senior Platform Engineer",
    )
    return RawJob(**{**defaults, **kw})


@pytest.fixture
def cfg() -> Config:
    return Config.model_validate(
        {
            "home_country": "AU",
            "titles": {"include": ["platform", "infrastructure", "sre"], "exclude": ["sales", "manager"]},
        }
    )
