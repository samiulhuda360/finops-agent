from __future__ import annotations

import pandas as pd
import pytest

from finops_agent import focus
from finops_agent.generator import Dataset, generate


@pytest.fixture(scope="session")
def dataset() -> Dataset:
    return generate(0)


@pytest.fixture(scope="session")
def prepared(dataset: Dataset) -> pd.DataFrame:
    return focus.prepare(dataset.focus)
