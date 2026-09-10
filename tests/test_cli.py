"""Separate-client acceptance paths, also run against the installed wheel in CI."""

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from quail import kernel

PYTHON = os.environ.get("QUAIL_TEST_EXECUTABLE", sys.executable)


def command(directory, *arguments, check=True, timeout=20):
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [PYTHON, "-m", "quail.cli", *map(str, arguments)],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.fixture
def local_runtime():
    try:
        kernel._usage(os.getpid())
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM):
            pass
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as error:
        pytest.skip(f"Local host facilities unavailable in this environment: {error}")


@pytest.fixture
def cli_study(tmp_path, local_runtime):
    # Long parent paths exercise relative Unix socket bind/connect on both OSes.
    directory = tmp_path / ("study-" + "a" * 90) / ("data-" + "b" * 70)
    command(tmp_path, "init", directory)
    (directory / "notes.csv").write_text(
        "id,body\nn1,The parking permit is too expensive.\nn2,The staff were helpful.\n",
        encoding="utf-8",
    )
    command(directory, "import", "notes.csv")
    try:
        yield directory
    finally:
        for path in (directory / "sessions").iterdir():
            if path.is_dir():
                command(directory, "exec", path.name, "--close", check=False)


def test_repository_or_wheel_cli_import_and_manual(tmp_path):
    command(tmp_path, "init", tmp_path / "study")
    directory = tmp_path / "study"
    (directory / "notes.csv").write_text("id,body\na,café\n", encoding="utf-8")
    command(directory, "import", "notes.csv")
    info = json.loads(command(directory, "info", "--json").stdout)
    assert info["datasets"][0]["rows"] == 1 and info["sessions"] == []
    assert info["interface"]["exec"].startswith(PYTHON)
    result = subprocess.run(
        [PYTHON, "-c", "from quail.service import usage_manual; print(usage_manual(), end='')"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=True,
    )
    expected = (Path(__file__).resolve().parents[1] / "USING_QUAIL.md").read_text(encoding="utf-8")
    assert result.stdout == expected


@pytest.fixture
def embedding_server(local_runtime):
    requests = []
    entered, release = threading.Event(), threading.Event()
    release.set()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            document = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/api/embed" and document["truncate"] is False
            texts = document["input"]
            requests.append(texts)
            entered.set()
            assert release.wait(timeout=20)
            vectors = [[1, 0] if "parking" in text.lower() else [0, 1] for text in texts]
            payload = json.dumps({"embeddings": vectors}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, entered, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def semantic_configuration(directory, address):
    manifest = directory / "quail.toml"
    manifest.write_text(
        manifest.read_text() + '\nembed = "ollama/test"\nembed_revision = "v1"\n'
        f'\n[providers.ollama]\nbase_url = "{address}"\n'
    )


def test_shared_warming_and_cold_clone_semantics_from_separate_clients(
    cli_study, embedding_server, tmp_path
):
    address, requests, _, _ = embedding_server
    semantic_configuration(cli_study, address)
    for shard in ("1/2", "2/2"):
        result = json.loads(
            command(
                cli_study, "warm", "notes", "--field", "body", "--shard", shard, "--json"
            ).stdout
        )
        assert result["selected"] == 1 and result["pack"] is not None
    assert not list((cli_study / "sessions").iterdir())
    destination = tmp_path / "cloned-study"
    shutil.copytree(cli_study, destination, ignore=shutil.ignore_patterns(".quail"))
    before = len(requests)
    try:
        first = command(
            destination,
            "exec",
            "review",
            "-c",
            'score = Field("body").semantic("parking"); count(score > 0.5)',
            "--json",
        )
        assert json.loads(first.stdout)["output"] == "1\n"
        assert requests[before:] == [["parking"]]  # The corpus came entirely from the shared parts.
        assert (
            command(destination, "exec", "review", "-c", "retrieve(rank=score)[0][score]").stdout
            == "1.0\n"
        )
        assert requests[before:] == [["parking"]]
        command(destination, "exec", "review", "--reset")
        assert (
            command(
                destination,
                "exec",
                "review",
                "-c",
                'count(Field("body").semantic("parking") > 0.5)',
            ).stdout
            == "1\n"
        )
        assert requests[before:] == [["parking"]]
    finally:
        command(destination, "exec", "review", "--close", check=False)


def test_local_status_and_another_session_remain_available_during_provider_wait(
    cli_study, embedding_server
):
    address, _, entered, release = embedding_server
    semantic_configuration(cli_study, address)
    release.clear()
    client = subprocess.Popen(
        [
            PYTHON,
            "-m",
            "quail.cli",
            "exec",
            "review",
            "-c",
            'count(Field("body").semantic("parking") > 0.5)',
            "--json",
        ],
        cwd=cli_study,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert entered.wait(timeout=15)
        info = json.loads(command(cli_study, "info", "--json").stdout)
        assert info["sessions"][0]["runtime"]["state"] == "busy"
        busy = command(cli_study, "exec", "review", "--reset", check=False)
        assert busy.returncode != 0 and "busy" in busy.stderr
        assert (
            command(cli_study, "exec", "other", "-c", 'tag(None, "checked", True)').stdout == "2\n"
        )
        release.set()
        output, errors = client.communicate(timeout=20)
        assert client.returncode == 0, output + errors
        assert json.loads(output)["output"] == "1\n"
    finally:
        release.set()
        if client.poll() is None:
            client.terminate()
            client.communicate(timeout=5)


def test_download_to_analysis_without_info_and_persistent_files(cli_study):
    first = command(
        cli_study,
        "exec",
        "first-pass",
        "-c",
        'body = Field("body"); parking = body.lexical("parking") > 0; count(parking)',
    )
    assert first.stdout == "1\n"
    second = command(
        cli_study,
        "exec",
        "first-pass",
        "-c",
        'tag(parking, "topic", "parking"); count(by=Field("topic"))',
    )
    assert second.stdout == "Counter({'parking': 1, None: 1})\n"
    script = cli_study / "helpers.py"
    code = (
        "from dataclasses import dataclass\n@dataclass\nclass Theme:\n"
        "    label: str\n    def size(self): return count(parking)\n"
        'theme = Theme("café 🐦")\ntheme.label\n'
    )
    script.write_text(code, encoding="utf-8")
    assert command(cli_study, "exec", "first-pass", script).stdout == "'café 🐦'\n"
    assert command(cli_study, "exec", "first-pass", "-c", "theme.size()").stdout == "1\n"
    command(cli_study, "export", "first-pass")
    command(cli_study, "exec", "first-pass", "--close")
    result = command(
        cli_study, "exec", "first-pass", "-c", 'count(Field("topic") == "parking")', "--json"
    )
    assert json.loads(result.stdout)["output"] == "1\n"
    records = [
        json.loads(line)
        for path in (cli_study / "sessions/first-pass/log").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert any(record.get("code") == code for record in records)


def test_failure_reset_close_and_socket_cleanup(cli_study):
    first = json.loads(command(cli_study, "exec", "review", "-c", "saved = 2", "--json").stdout)
    failed = command(
        cli_study,
        "exec",
        "review",
        "-c",
        'saved += 1; print("kept"); tag(None, "lost", True); 1 / 0',
        "--json",
        check=False,
    )
    assert failed.returncode == 1
    assert json.loads(failed.stdout)["error"]["type"] == "ZeroDivisionError"
    assert "kept" in json.loads(failed.stdout)["output"]
    assert command(cli_study, "exec", "review", "-c", "saved").stdout == "3\n"
    endpoint = next((cli_study / ".quail/run").glob("*/kernel.sock"))
    assert stat.S_IMODE(endpoint.stat().st_mode) == 0o600
    reset = json.loads(command(cli_study, "exec", "review", "--reset", "--json").stdout)
    assert reset["reset"] is True and reset["session"] == "review"
    assert reset["state"] == "idle" and reset["run"] != first["run"]
    assert reset["warnings"] == [] and reset["limits"] == first["limits"]
    assert command(cli_study, "exec", "review", "-c", "saved", check=False).returncode == 1
    closed = json.loads(command(cli_study, "exec", "review", "--close", "--json").stdout)
    assert closed == {"closed": True, "session": "review", "state": "stopped"}
    assert not endpoint.exists()
    assert not list((cli_study / ".quail/children").glob("kernel-*"))
    assert json.loads(command(cli_study, "exec", "review", "--close", "--json").stdout) == closed
    assert not endpoint.exists()


def _status(directory, session):
    result = json.loads(command(directory, "info", "--json").stdout)
    return next(item["runtime"] for item in result["sessions"] if item["name"] == session)


def _wait_status(directory, session, predicate):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = _status(directory, session)
        if predicate(status):
            return status
        time.sleep(0.05)
    pytest.fail(f"Runtime transition did not complete: {status}")


def test_disconnected_client_finishes_and_status_remains_responsive(cli_study):
    command(cli_study, "exec", "review", "-c", "pass")
    client = subprocess.Popen(
        [
            PYTHON,
            "-m",
            "quail.cli",
            "exec",
            "review",
            "-c",
            'import time; time.sleep(1.5); tag(None, "finished", True)',
        ],
        cwd=cli_study,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        busy = _wait_status(
            cli_study,
            "review",
            lambda value: value["state"] == "busy" and value["cell"] is not None,
        )
        for option in ("--reset", "--close"):
            conflict = command(cli_study, "exec", "review", option, check=False)
            assert conflict.returncode == 1 and "busy" in conflict.stderr
        client.terminate()
        client.communicate(timeout=3)
        completed = _wait_status(cli_study, "review", lambda value: value["state"] == "idle")
        assert completed["last_completed"] == {"run": busy["run"], "cell": busy["cell"]}
        assert (
            command(cli_study, "exec", "review", "-c", 'count(Field("finished") == True)').stdout
            == "2\n"
        )
    finally:
        if client.poll() is None:
            client.kill()
            client.communicate(timeout=3)


def test_concurrent_start_does_not_create_competing_kernels(cli_study):
    args = [PYTHON, "-m", "quail.cli", "exec", "review", "-c", "import os; os.getpid()"]
    clients = [
        subprocess.Popen(
            args, cwd=cli_study, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(2)
    ]
    outcomes = [(client, client.communicate(timeout=20)) for client in clients]
    successful = [int(output) for client, (output, _) in outcomes if client.returncode == 0]
    assert successful
    assert len(set(successful)) == 1
    assert (
        int(command(cli_study, "exec", "review", "-c", "import os; os.getpid()").stdout)
        == successful[0]
    )


def test_fork_from_and_reset_stopped_use_one_new_run(cli_study):
    command(cli_study, "exec", "original", "-c", 'tag(None, "theme", "parking")')
    command(cli_study, "exec", "original", "--close")
    result = command(
        cli_study,
        "exec",
        "copy",
        "--fork-from",
        "original",
        "-c",
        'count(Field("theme") == "parking")',
    )
    assert result.stdout == "2\n"
    conflict = command(
        cli_study, "exec", "copy", "--fork-from", "original", "-c", "count()", check=False
    )
    assert conflict.returncode == 1
    command(cli_study, "exec", "copy", "--close")
    directory = cli_study / "sessions/copy/log"
    before = len(list(directory.glob("*.jsonl")))
    interrupted = sorted(directory.glob("*.jsonl"))[-1]
    with interrupted.open("ab") as stream:
        stream.write(b'{"n":')
    reset = json.loads(command(cli_study, "exec", "copy", "--reset", "--json").stdout)
    assert reset["reset"] is True and reset["session"] == "copy" and reset["state"] == "idle"
    assert reset["limits"]["wall_seconds"] == 120
    assert any(interrupted.name in warning for warning in reset["warnings"])
    assert (directory / (reset["run"] + ".jsonl")).is_file()
    assert len(list(directory.glob("*.jsonl"))) == before + 1


def test_argument_and_file_errors_do_not_start_a_host(tmp_path):
    command(tmp_path, "init", tmp_path / "study")
    study = tmp_path / "study"
    for args in (
        ("-c", "pass", "other.py"),
        ("--reset", "--dataset", "notes"),
        ("missing.py",),
        (),
    ):
        assert command(study, "exec", "review", *args, check=False).returncode != 0
    assert not list((study / "sessions").iterdir())
