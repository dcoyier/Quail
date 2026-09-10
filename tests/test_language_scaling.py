"""Regression checks for repeated work, without machine-specific timing gates."""

import csv

from quail import project
from quail.index import Index
from quail.language.evaluator import Evaluator
from quail.language.expressions import constructors
from quail.language.state import State


def test_entry_expression_lookup_does_not_scan_the_corpus(tmp_path):
    work = []
    for count in (32, 512):
        study = project.initialize(tmp_path / str(count))
        source = study.root / "notes.csv"
        with source.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["id", "body"])
            writer.writerows((str(i), "parking") for i in range(count))
        text = project.registration_text(study, "notes", source)
        project.atomic_write(study.manifest, text.encode())
        study = project.load(study.root)
        with Index.build(study.index_path("notes"), study.dataset("notes")) as index:
            engine = Evaluator(State(index.path, index.source, "s", study.limits))
            field, _ = constructors(engine.state)
            try:
                engine.begin()
                score = field("body").lexical("parking")
                entry = engine.retrieve(rank=score, limit=1)[0]
                steps = 0

                def progress():
                    nonlocal steps
                    steps += 1
                    return 0

                engine.state.connection.set_progress_handler(progress, 10)
                for _ in range(10):
                    assert entry[score] == entry.score
                work.append(steps)
            finally:
                engine.close()
    # Sixteen times more rows must not require a comparable increase in VM work
    # for the same indexed Entry lookups. This leaves query-plan details open.
    assert work[1] < work[0] * 2
