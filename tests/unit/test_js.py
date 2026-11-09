import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.conftest import REPO_ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_frontend_helper_tests_pass():
    result = subprocess.run(
        ["node", "--test", str(Path(REPO_ROOT, "tests", "js"))],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
