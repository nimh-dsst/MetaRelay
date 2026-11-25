"""The first_write example, run as the README shows: a script in an empty folder."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "examples" / "first_write.py"


def test_writes_the_record_and_prints_it_back(tmp_path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "{'fiber_diameter_um': 400.0, 'target_region': 'VTA'}\n"
    assert (tmp_path / "metadata" / "Fiber study" / "Cohort A" / "implant-M1.json").is_file()
