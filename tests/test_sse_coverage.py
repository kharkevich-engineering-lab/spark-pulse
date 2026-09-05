"""What the SSE generators actually put on the wire.

``tests/test_sse_streams.py`` guards the two things that are invisible from
outside — no blocking call on the event loop, no import form that defeats
simulation mode. This file drives the generators themselves: the exact frames
they emit, what happens when a producer raises, and what happens when the
browser goes away mid-stream.

Everything here runs the generators directly rather than over HTTP, because a
stream only ends when the client disconnects and a test client that never
disconnects would hang. Nothing sleeps: the ``sleeps`` fixture swaps
``asyncio.sleep`` for a yield to the loop and records what the interval would
have been, so the tick intervals are asserted rather than waited out.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from spark_pulse import sse
from spark_pulse import tools
from spark_pulse.tools.events import DeploymentEvent, EventBroadcaster, EventType


# Captured before any test can replace ``asyncio.sleep``.
_REAL_SLEEP = asyncio.sleep


# ── Helpers ──────────────────────────────────────────────────────────────────


def parse(frame: str) -> tuple[str | None, dict]:
    """Split one SSE frame into (event name or None, decoded payload).

    Also asserts the frame is well formed: one trailing blank line, a single
    ``data:`` line, and JSON the browser can actually parse.
    """
    assert frame.endswith("\n\n"), f"frame is not terminated: {frame!r}"
    lines = frame[:-2].split("\n")
    event = None
    if lines[0].startswith("event: "):
        event = lines[0][len("event: ") :]
        lines = lines[1:]
    assert len(lines) == 1, f"frame has {len(lines)} data lines: {frame!r}"
    assert lines[0].startswith("data: "), f"no data line: {frame!r}"
    return event, json.loads(lines[0][len("data: ") :])


async def take(agen, count: int, timeout: float = 2.0) -> list[str]:
    """Pull up to ``count`` frames, leaving the generator suspended."""
    frames: list[str] = []

    async def _run():
        async for chunk in agen:
            frames.append(chunk)
            if len(frames) >= count:
                return

    await asyncio.wait_for(_run(), timeout)
    return frames


async def drain(agen, timeout: float = 2.0) -> list[str]:
    """Run the generator to completion. Fails loudly if it never ends."""
    frames: list[str] = []

    async def _run():
        async for chunk in agen:
            frames.append(chunk)
            assert len(frames) < 200, "stream did not end"

    await asyncio.wait_for(_run(), timeout)
    return frames


async def until(predicate, timeout: float = 2.0) -> None:
    """Spin the loop until ``predicate()`` holds."""

    async def _spin():
        while not predicate():
            await _REAL_SLEEP(0)

    await asyncio.wait_for(_spin(), timeout)


async def pump(agen, frames: list[str]) -> None:
    """Forward every frame into ``frames`` until cancelled."""
    async for chunk in agen:
        frames.append(chunk)


@pytest.fixture
def sleeps(monkeypatch):
    """Replace every await on ``asyncio.sleep`` with a yield to the loop."""
    recorded: list[float] = []

    async def _instant(delay, *args, **kwargs):
        recorded.append(delay)
        await _REAL_SLEEP(0)

    monkeypatch.setattr(asyncio, "sleep", _instant)
    return recorded


@pytest.fixture
def broadcaster(monkeypatch):
    """A broadcaster of this test's own, not the process-wide one."""
    monkeypatch.setattr(sse, "_event_broadcaster", None)
    return sse._get_event_broadcaster()


@pytest.fixture
def loops(monkeypatch):
    """Record the loop each catalogue tool is handed, and never keep it.

    The generators register the running loop with modules that outlive the
    test; left registered, a later emit would target a closed loop.
    """
    registered: dict[str, object] = {}

    for name in ("models", "images", "native_runtime"):
        module = getattr(tools, name)
        monkeypatch.setattr(
            module,
            "register_event_loop",
            lambda loop, _name=name: registered.__setitem__(_name, loop),
        )
    return registered


def model_event(resource: str = "qwen") -> DeploymentEvent:
    return DeploymentEvent(
        event_type=EventType.MODEL_DOWNLOAD_PROGRESS,
        resource=resource,
        resource_type="model",
        message="downloading",
        metadata={"percent": 12},
    )


def image_event(resource: str = "vllm:0.1.0") -> DeploymentEvent:
    return DeploymentEvent(
        event_type=EventType.IMAGE_PULL_PROGRESS,
        resource=resource,
        resource_type="image",
        message="pulling",
        metadata={"percent": 40},
    )


def deployment_event(resource: str = "d1") -> DeploymentEvent:
    return DeploymentEvent(
        event_type=EventType.DEPLOYMENT_READY,
        resource=resource,
        resource_type="deployment",
        message="ready",
    )


# ── /sse/metrics ─────────────────────────────────────────────────────────────


@pytest.fixture
def metrics_source(monkeypatch):
    """Canned memory, canned deployment records, and a recording enricher."""
    state = {
        "memory": {
            "system": {"used_gb": 12},
            "processes": [{"pid": 7, "used_mb": 900}],
        },
        "records": [
            {"id": "run-1", "status": "running"},
            {"id": "pend-1", "status": "pending"},
            {"id": "stop-1", "status": "stopped"},
            {"id": "err-1", "status": "error"},
        ],
        "enriched": [],
    }

    def _memory():
        if isinstance(state["memory"], Exception):
            raise state["memory"]
        return json.loads(json.dumps(state["memory"]))

    def _enrich(processes, running):
        state["enriched"].append([d["id"] for d in running])
        for proc in processes:
            proc["deployment"] = "run-1"

    monkeypatch.setattr(sse.system, "get_all_memory", _memory)
    monkeypatch.setattr(sse.system, "enrich_gpu_process_tracking", _enrich)
    monkeypatch.setattr(
        tools.deployment_records, "load", lambda: list(state["records"])
    )
    return state


class TestMetricsStream:
    """The stream the monitoring page lives on."""

    async def test_the_first_frame_carries_the_enriched_snapshot(
        self, sleeps, metrics_source
    ):
        agen = sse.metrics_generator()

        (frame,) = await take(agen, 1)
        await agen.aclose()

        assert frame == (
            "event: metrics\n"
            'data: {"system": {"used_gb": 12}, '
            '"processes": [{"pid": 7, "used_mb": 900, "deployment": "run-1"}]}'
            "\n\n"
        )
        event, payload = parse(frame)
        assert event == "metrics"
        assert payload["processes"][0]["deployment"] == "run-1"

    async def test_only_live_deployments_are_offered_for_gpu_attribution(
        self, sleeps, metrics_source
    ):
        """A stopped deployment cannot own a GPU process any more."""
        agen = sse.metrics_generator()

        await take(agen, 1)
        await agen.aclose()

        assert metrics_source["enriched"] == [["run-1", "pend-1"]]

    async def test_collection_happens_off_the_event_loop(
        self, sleeps, metrics_source, monkeypatch
    ):
        """nvidia-smi and the record store would stall every other stream."""
        seen: list[int] = []
        original = sse.system.get_all_memory

        def _record():
            seen.append(threading.get_ident())
            return original()

        monkeypatch.setattr(sse.system, "get_all_memory", _record)

        agen = sse.metrics_generator()
        await take(agen, 1)
        await agen.aclose()

        assert seen and seen[0] != threading.get_ident()

    async def test_a_failed_collection_becomes_an_error_frame(
        self, sleeps, metrics_source
    ):
        metrics_source["memory"] = RuntimeError("nvidia-smi: no devices found")
        agen = sse.metrics_generator()

        (frame,) = await take(agen, 1)
        await agen.aclose()

        assert parse(frame) == (
            "error",
            {"message": "nvidia-smi: no devices found"},
        )

    async def test_an_error_message_is_encoded_not_interpolated(
        self, sleeps, metrics_source
    ):
        """Regression: a quote made the frame unparseable, a newline split it.

        The message was pasted into the JSON by hand, so anything docker or
        nvidia-smi said with a quote or a line break in it arrived at the
        browser as a broken frame.
        """
        metrics_source["memory"] = RuntimeError('Container "qwen" died\nexit 137')
        agen = sse.metrics_generator()

        (frame,) = await take(agen, 1)
        await agen.aclose()

        assert frame.count("\n\n") == 1
        assert parse(frame) == (
            "error",
            {"message": 'Container "qwen" died\nexit 137'},
        )

    async def test_a_failure_does_not_wedge_the_stream(
        self, sleeps, metrics_source, monkeypatch
    ):
        """One bad reading, then back to metrics — the stream must survive."""
        calls = {"n": 0}
        good = metrics_source["memory"]

        def _flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return json.loads(json.dumps(good))

        monkeypatch.setattr(sse.system, "get_all_memory", _flaky)

        agen = sse.metrics_generator()
        first, second = await take(agen, 2)
        await agen.aclose()

        assert parse(first) == ("error", {"message": "transient"})
        assert parse(second)[0] == "metrics"

    async def test_the_stream_ticks_every_five_seconds(self, sleeps, metrics_source):
        agen = sse.metrics_generator()

        await take(agen, 3)
        await agen.aclose()

        assert sleeps == [5, 5]

    async def test_a_disconnect_stops_the_collection(self, sleeps, metrics_source):
        """Closing the stream must stop the work, not just the frames."""
        agen = sse.metrics_generator()
        await take(agen, 1)

        await agen.aclose()
        before = len(metrics_source["enriched"])
        with pytest.raises(StopAsyncIteration):
            await agen.__anext__()

        assert len(metrics_source["enriched"]) == before == 1


# ── /sse/logs/{deployment_id} ────────────────────────────────────────────────


@pytest.fixture
def dispatch(monkeypatch):
    """A scriptable stand-in for ``tools.deploy_dispatch``."""
    state = {"deployments": [], "logs": [], "records": []}

    def _list():
        # The real dispatcher rebuilds these dicts from the record store on
        # every call, so a generator holding one holds a snapshot.
        return [dict(record) for record in state["deployments"]]

    def _get_logs(_deployment_id, _lines):
        state.setdefault("log_threads", []).append(threading.get_ident())
        return state["logs"].pop(0) if len(state["logs"]) > 1 else state["logs"][0]

    def _get_deployment(_deployment_id):
        return (
            state["records"].pop(0)
            if len(state["records"]) > 1
            else (state["records"][0] if state["records"] else None)
        )

    monkeypatch.setattr(tools.deploy_dispatch, "list_deployments", _list)
    monkeypatch.setattr(tools.deploy_dispatch, "get_logs", _get_logs)
    monkeypatch.setattr(tools.deploy_dispatch, "get_deployment", _get_deployment)
    return state


class TestFileLogStream:
    """Tailing the log file a container deployment writes."""

    async def test_an_unknown_deployment_gets_one_error_frame(self, sleeps, dispatch):
        frames = await drain(sse.log_generator("ghost"))

        assert len(frames) == 1
        assert parse(frames[0]) == ("error", {"message": "Deployment not found"})

    async def test_a_deployment_without_a_log_file_says_so(self, sleeps, dispatch):
        dispatch["deployments"] = [{"id": "d1", "status": "running"}]

        frames = await drain(sse.log_generator("d1"))

        assert len(frames) == 1
        assert parse(frames[0]) == (
            "error",
            {"message": "No log file for this deployment"},
        )

    async def test_existing_lines_are_replayed_then_new_ones_tailed(
        self, sleeps, dispatch, tmp_path
    ):
        log = tmp_path / "d1.log"
        log.write_text("first\n\nsecond\n")
        record = {"id": "d1", "status": "running", "log_path": str(log)}
        dispatch["deployments"] = [record]

        agen = sse.log_generator("d1")
        replayed = await take(agen, 2)

        # The blank line is not a log line.
        assert [parse(f) for f in replayed] == [
            ("log", {"text": "first"}),
            ("log", {"text": "second"}),
        ]

        record["status"] = "stopped"
        rest = await drain(agen)

        assert [parse(f) for f in rest] == [("status", {"status": "stopped"})]

    async def test_the_tail_polls_twice_a_second(self, sleeps, dispatch, tmp_path):
        log = tmp_path / "d1.log"
        log.write_text("only\n")
        record = {"id": "d1", "status": "running", "log_path": str(log)}
        dispatch["deployments"] = [record]

        agen = sse.log_generator("d1")
        await take(agen, 1)
        record["status"] = "error"
        await drain(agen)

        assert sleeps == [0.5]

    async def test_a_status_change_is_announced_before_the_stream_ends(
        self, sleeps, dispatch, tmp_path
    ):
        log = tmp_path / "d1.log"
        log.write_text("boot\n")
        record = {"id": "d1", "status": "pending", "log_path": str(log)}
        dispatch["deployments"] = [record]

        agen = sse.log_generator("d1")
        assert [parse(f) for f in await take(agen, 1)] == [("log", {"text": "boot"})]

        record["status"] = "running"
        running = await take(agen, 1)
        assert [parse(f) for f in running] == [("status", {"status": "running"})]

        # Now that the generator is polling, a line appended to the file is
        # picked up from the offset it stopped at rather than replayed whole.
        with log.open("a") as handle:
            handle.write("serving\n")
        assert [parse(f) for f in await take(agen, 1)] == [("log", {"text": "serving"})]

        record["status"] = "error"
        rest = await drain(agen)

        assert [parse(f) for f in rest] == [("status", {"status": "error"})]

    async def test_a_deleted_deployment_ends_the_stream_quietly(
        self, sleeps, dispatch, tmp_path
    ):
        log = tmp_path / "d1.log"
        log.write_text("boot\n")
        dispatch["deployments"] = [
            {"id": "d1", "status": "running", "log_path": str(log)}
        ]

        agen = sse.log_generator("d1")
        await take(agen, 1)
        dispatch["deployments"] = []

        assert await drain(agen) == []

    async def test_a_deployment_that_already_failed_emits_nothing_further(
        self, sleeps, dispatch, tmp_path
    ):
        """No file yet and a terminal status: one poll, then done."""
        dispatch["deployments"] = [
            {
                "id": "d1",
                "status": "error",
                "log_path": str(tmp_path / "never-written.log"),
            }
        ]

        assert await drain(sse.log_generator("d1")) == []


class TestNativeLogStream:
    """A native deployment writes to docker's stdout, not to a file."""

    async def test_docker_logs_are_diffed_against_what_was_already_sent(
        self, sleeps, dispatch
    ):
        dispatch["deployments"] = [{"id": "d1", "runtime": "native"}]
        dispatch["logs"] = ["alpha\nbeta\n", "alpha\nbeta\ngamma\n"]
        dispatch["records"] = [{"status": "running"}, {"status": "stopped"}]

        frames = await drain(sse.log_generator("d1"))

        assert [parse(f) for f in frames] == [
            ("log", {"text": "alpha"}),
            ("log", {"text": "beta"}),
            ("status", {"status": "running"}),
            ("log", {"text": "gamma"}),
            ("status", {"status": "stopped"}),
        ]

    async def test_the_poll_interval_is_two_seconds(self, sleeps, dispatch):
        dispatch["deployments"] = [{"id": "d1", "runtime": "native"}]
        dispatch["logs"] = ["alpha\n", "alpha\n"]
        dispatch["records"] = [{"status": "running"}, {"status": "error"}]

        await drain(sse.log_generator("d1"))

        assert sleeps == [2]

    async def test_a_vanished_record_ends_the_stream_without_a_status_frame(
        self, sleeps, dispatch
    ):
        dispatch["deployments"] = [{"id": "d1", "runtime": "native"}]
        dispatch["logs"] = ["alpha\n"]
        dispatch["records"] = []

        frames = await drain(sse.log_generator("d1"))

        assert [parse(f) for f in frames] == [("log", {"text": "alpha"})]

    async def test_docker_is_reached_off_the_event_loop(self, sleeps, dispatch):
        """These calls go over SSH for a remote node — never on the loop."""
        dispatch["deployments"] = [{"id": "d1", "runtime": "native"}]
        dispatch["logs"] = ["alpha\n"]
        dispatch["records"] = [{"status": "stopped"}]

        await drain(sse.log_generator("d1"))

        assert dispatch["log_threads"]
        assert threading.get_ident() not in dispatch["log_threads"]


# ── /sse/models, /sse/images, /sse/events/deployments ────────────────────────


class TestCatalogueStreams:
    """Model and image progress, filtered out of the one broadcaster."""

    async def test_the_models_stream_forwards_only_model_events(
        self, broadcaster, loops
    ):
        frames: list[str] = []
        task = asyncio.create_task(pump(sse.models_generator(), frames))
        await until(lambda: broadcaster.subscriber_count == 1)

        await broadcaster.emit(image_event())
        await broadcaster.emit(deployment_event())
        await broadcaster.emit(model_event("qwen"))
        await until(lambda: frames)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert len(frames) == 1
        event, payload = parse(frames[0])
        # The catalogue streams send unnamed frames; the type is in the body.
        assert event is None
        assert payload["type"] == "model.download.progress"
        assert payload["resource"] == "qwen"
        assert payload["metadata"] == {"percent": 12}

    async def test_the_images_stream_forwards_only_image_events(
        self, broadcaster, loops
    ):
        frames: list[str] = []
        task = asyncio.create_task(pump(sse.images_generator(), frames))
        await until(lambda: broadcaster.subscriber_count == 1)

        await broadcaster.emit(model_event())
        await broadcaster.emit(image_event("vllm:0.1.0"))
        await until(lambda: frames)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert len(frames) == 1
        _, payload = parse(frames[0])
        assert payload["type"] == "image.pull.progress"
        assert payload["resource"] == "vllm:0.1.0"

    async def test_the_deployment_stream_forwards_everything(self, broadcaster, loops):
        """Deploy-time pulls belong to the deployment's own timeline."""
        frames: list[str] = []
        task = asyncio.create_task(pump(sse.deployment_events_generator(), frames))
        await until(lambda: broadcaster.subscriber_count == 1)

        await broadcaster.emit(deployment_event("d1"))
        await broadcaster.emit(image_event())
        await until(lambda: len(frames) == 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert [parse(f)[1]["resource_type"] for f in frames] == [
            "deployment",
            "image",
        ]

    @pytest.mark.parametrize(
        "generator, tool",
        [
            ("models_generator", "models"),
            ("images_generator", "images"),
            ("deployment_events_generator", "native_runtime"),
        ],
    )
    async def test_the_running_loop_is_handed_to_the_worker_threads(
        self, broadcaster, loops, generator, tool
    ):
        """Jobs emit from worker threads; without the loop nothing arrives."""
        frames: list[str] = []
        task = asyncio.create_task(pump(getattr(sse, generator)(), frames))
        await until(lambda: broadcaster.subscriber_count == 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert loops[tool] is asyncio.get_running_loop()

    @pytest.mark.parametrize(
        "generator",
        ["models_generator", "images_generator", "deployment_events_generator"],
    )
    async def test_a_disconnect_unsubscribes(self, broadcaster, loops, generator):
        """A leaked queue per reconnect is an unbounded memory leak."""
        frames: list[str] = []
        task = asyncio.create_task(pump(getattr(sse, generator)(), frames))
        await until(lambda: broadcaster.subscriber_count == 1)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert broadcaster.subscriber_count == 0

    async def test_every_stream_shares_one_broadcaster(self, broadcaster, loops):
        """Separate broadcasters would mean nothing was ever delivered."""
        model_frames: list[str] = []
        deployment_frames: list[str] = []
        tasks = [
            asyncio.create_task(pump(sse.models_generator(), model_frames)),
            asyncio.create_task(
                pump(sse.deployment_events_generator(), deployment_frames)
            ),
        ]
        await until(lambda: broadcaster.subscriber_count == 2)

        await broadcaster.emit(model_event("qwen"))
        await until(lambda: model_frames and deployment_frames)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        assert model_frames == deployment_frames

    def test_the_broadcaster_is_created_once(self, monkeypatch):
        monkeypatch.setattr(sse, "_event_broadcaster", None)

        first = sse._get_event_broadcaster()

        assert isinstance(first, EventBroadcaster)
        assert sse._get_event_broadcaster() is first


# ── Endpoint wiring ──────────────────────────────────────────────────────────


class TestEndpoints:
    """Each route must hand back its own generator, unbuffered."""

    def test_the_advertised_paths(self):
        assert {route.path for route in sse.router.routes} == {
            "/sse/metrics",
            "/sse/logs/{deployment_id}",
            "/sse/models",
            "/sse/images",
            "/sse/events/deployments",
        }

    @pytest.mark.parametrize(
        "endpoint, generator",
        [
            ("sse_metrics", "metrics_generator"),
            ("sse_models", "models_generator"),
            ("sse_images", "images_generator"),
            ("sse_deployment_events", "deployment_events_generator"),
        ],
    )
    async def test_each_endpoint_streams_its_own_generator(self, endpoint, generator):
        response = await getattr(sse, endpoint)()

        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["connection"] == "keep-alive"
        # Without this nginx buffers the stream into uselessness.
        assert response.headers["x-accel-buffering"] == "no"
        assert response.body_iterator.__name__ == generator
        await response.body_iterator.aclose()

    async def test_the_log_endpoint_streams_the_requested_deployment(
        self, sleeps, dispatch
    ):
        response = await sse.sse_logs("ghost")

        assert response.media_type == "text/event-stream"
        frames = await drain(response.body_iterator)
        assert parse(frames[0]) == ("error", {"message": "Deployment not found"})
