import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from creditmemo import extract, pipeline, synth  # noqa: E402


@pytest.fixture(scope="session")
def data_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("sample_data")
    synth.generate(root)
    return root


@pytest.fixture(scope="session")
def extracted(data_root):
    return {slug: extract.extract_borrower(data_root, slug) for slug in synth.BORROWERS}


@pytest.fixture(scope="session")
def run_out(data_root, tmp_path_factory):
    out = tmp_path_factory.mktemp("out")
    summaries = pipeline.run(data_root, out, ROOT / "policy.yaml")
    return out, {s["slug"]: s for s in summaries}
