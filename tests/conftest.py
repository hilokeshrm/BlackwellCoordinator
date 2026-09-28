import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import sample_projects  # noqa: E402


@pytest.fixture
def raw_finetune(tmp_path):
    return sample_projects.make_finetune(tmp_path / "quick-finetune", patched=False)


@pytest.fixture
def finetune(tmp_path):
    return sample_projects.make_finetune(tmp_path / "quick-finetune", patched=True)


@pytest.fixture
def pretrain(tmp_path):
    return sample_projects.make_pretrain(tmp_path / "long-pretrain")
