import json
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module, forbidden",
    [
        ("quail.contracts", ["quail.project", "quail.history", "quail.index", "numpy"]),
        (
            "quail.prelude",
            ["quail.project", "quail.history", "quail.index", "quail.kernel", "quail.embed"],
        ),
        (
            "quail.language.evaluator",
            ["quail.project", "quail.history", "quail.index", "quail.kernel", "quail.embed"],
        ),
        ("quail.cli", ["quail.kernel", "quail.prelude", "numpy"]),
        ("quail.service", ["quail.kernel", "quail.prelude", "numpy"]),
        ("quail.packs", ["quail.kernel", "quail.prelude", "quail.embed", "numpy"]),
    ],
)
def test_import_boundaries(module, forbidden):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import {module}; import sys,json; print(json.dumps(list(sys.modules)))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert not set(forbidden).intersection(json.loads(result.stdout))
