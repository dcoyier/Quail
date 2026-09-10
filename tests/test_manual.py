"""Execute the canonical manual's runnable Python, not a copied set of examples."""

import csv
import re
from dataclasses import FrozenInstanceError

import pytest

from quail import embed, service
from quail.contracts import FieldInfo
from quail.index import Index
from quail.language.evaluator import Evaluator
from quail.language.state import State
from quail.prelude import Runner
from quail.project import EmbeddingConfig


def test_manual_reference_and_continuous_example_execute_with_absent_values(study):
    body = "Parking permits and hydrangeas need attention. " * 16
    rows = [
        ["a", body, "parking", "support", "2024", "35"],
        ["b", "The staff were helpful.", "office", "sales", "2023", "31"],
        ["c", "", "", "", "", "unknown"],
        ["d", "There is no place to leave a car near work.", "access", "support", "2025", "40"],
    ]
    with study.dataset("notes").source.open("w", newline="") as stream:
        csv.writer(stream).writerows([["id", "body", "title", "dept", "year", "age"], *rows])
    config = EmbeddingConfig(
        "ollama/manual-test", "v1", "ollama", "manual-test", "http://unused.invalid"
    )

    def raw(configuration, texts):
        return [[1 if "park" in text.lower() or "car" in text.lower() else 0, 1] for text in texts]

    with Index.build(study.index_path("notes"), study.dataset("notes")) as index:
        cache = embed.Cache(index, config, raw=raw)
        runner = Runner(
            Evaluator(
                State(index.path, index.source, "manual", study.limits),
                embedding_id=config.identity,
                embed=lambda texts: cache.get(texts).vectors,
            ),
            timers=False,
        )
        try:
            initial = runner.run(1, 'tag(Field("id") == "a", "topic", "billing")')
            assert initial.error is None
            snippets = re.findall(
                r"^```python\n(.*?)\n```", service.usage_manual(), re.MULTILINE | re.DOTALL
            )
            executed = 0
            for snippet in snippets:
                # The five verb signatures describe call shapes, not Python cells.
                if re.fullmatch(r"\w+\([^\n]*\) -> [^\n]+", snippet):
                    continue
                executed += 1
                reply = runner.run(executed + 1, snippet)
                assert reply.error is None, f"Manual snippet failed:\n{snippet}\n{reply.output}"
            assert executed >= 15
            namespace = runner.module.__dict__
            assert namespace["lengths"] == [len(row[1]) for row in rows if row[1]]
            assert namespace["sem"] is not None
            assert runner.evaluator.values(namespace["sem"])[2] is None
            catalog = runner.evaluator.fields()
            assert catalog[0] == FieldInfo("id", "source", 4)
            assert next(item for item in catalog if item.name == "body").present == 3
            assert next(item for item in catalog if item.name == "characters").kind == "tag"
            with pytest.raises(FrozenInstanceError):
                catalog[0].present = 0
        finally:
            runner.close()
