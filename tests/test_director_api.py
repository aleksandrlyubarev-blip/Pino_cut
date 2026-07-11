"""Tests for the Director-facing Engine HTTP API."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pinocut.director_api import create_app  # noqa: E402
from pinocut.scene_stitcher import SceneStitcherAgent  # noqa: E402
from pinocut.state import (  # noqa: E402
    ClipSegment,
    ExportProfile,
    SceneState,
    TimelineV1,
)


def make_state(
    scene_id: str, output_dir: str, *, clips: list[ClipSegment] | None = None
) -> SceneState:
    state = SceneState(
        project_id="proj",
        scene_id=scene_id,
        scene_goal="test goal",
        style_profile="cinematic",
        target_duration_sec=10.0,
        available_clips=clips or [],
        output_dir=output_dir,
    )
    state.timeline = TimelineV1(
        project_id="proj",
        scene_id=scene_id,
        timeline_version=1,
        scene_goal="test goal",
        style_profile="cinematic",
        editing_mode="hybrid",
        editing_template="cinematic_montage",
        target_duration_sec=10.0,
        export_profile=ExportProfile(),
    )
    state.approved_clips = [c.path.stem for c in state.available_clips]
    return state


class StubAgent(SceneStitcherAgent):
    """Replaces heavy pipeline calls with instant fakes; tool API stays real."""

    def __init__(self, tmp_path: Path, *, fail: bool = False):
        super().__init__()
        self.tmp_path = tmp_path
        self.fail = fail
        self.bassito_rounds = 0

    def build_from_request(self, request):  # noqa: ANN001
        if self.fail:
            raise RuntimeError("synthetic build failure")
        clip = ClipSegment(
            path=self.tmp_path / "clip_a.mp4",
            duration=5.0,
            has_audio=True,
            resolution=(1920, 1080),
            fps=24.0,
            metadata={},
        )
        state = make_state(request.scene_id, str(request.output_dir), clips=[clip])
        state.reviews["video_path"] = str(self.tmp_path / f"{request.scene_id}.mp4")
        return state

    def run_bassito_round(self, scene_state, *, runner=None):  # noqa: ANN001
        self.bassito_rounds += 1
        for job in list(scene_state.bridge_jobs) + list(scene_state.regeneration_jobs):
            job.status = "done"
            scene_state.bassito_history.append(job)
        scene_state.bridge_jobs = []
        scene_state.regeneration_jobs = []
        scene_state.timeline_version += 1
        return scene_state


def wait_status(
    client: TestClient, scene_id: str, *, until: set[str], timeout: float = 5.0
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get(f"/scenes/{scene_id}").json()
        if payload["status"] in until:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"scene {scene_id} never reached {until}: {payload}")


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(StubAgent(tmp_path)))


def build_scene(client: TestClient, tmp_path: Path, **overrides) -> str:
    body = {
        "clips_dir": str(tmp_path),
        "goal": "test goal",
        **overrides,
    }
    response = client.post("/scenes", json=body)
    assert response.status_code == 202, response.text
    scene_id = response.json()["scene_id"]
    wait_status(client, scene_id, until={"completed", "failed"})
    return scene_id


def test_health(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"


def test_build_scene_lifecycle(client: TestClient, tmp_path: Path) -> None:
    scene_id = build_scene(client, tmp_path)
    payload = client.get(f"/scenes/{scene_id}").json()
    assert payload["status"] == "completed"
    assert payload["scene_goal"] == "test goal"
    assert payload["approved_clips"] == ["clip_a"]
    assert payload["artifacts"]["video_path"].endswith(f"{scene_id}.mp4")
    assert client.get("/scenes").json()[0]["scene_id"] == scene_id


def test_build_rejects_missing_dir(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/scenes", json={"clips_dir": str(tmp_path / "nope"), "goal": "x"})
    assert response.status_code == 400


def test_build_failure_is_reported(tmp_path: Path) -> None:
    client = TestClient(create_app(StubAgent(tmp_path, fail=True)))
    response = client.post("/scenes", json={"clips_dir": str(tmp_path), "goal": "x"})
    scene_id = response.json()["scene_id"]
    payload = wait_status(client, scene_id, until={"failed"})
    assert "synthetic build failure" in payload["error"]


def test_fatal_pipeline_error_marks_scene_failed(tmp_path: Path) -> None:
    # Real agent + empty clips dir: build returns a fatal StageError, not an exception.
    client = TestClient(create_app(SceneStitcherAgent()))
    response = client.post("/scenes", json={"clips_dir": str(tmp_path), "goal": "x"})
    scene_id = response.json()["scene_id"]
    payload = wait_status(client, scene_id, until={"failed", "completed"})
    assert payload["status"] == "failed"
    assert "No source clips" in payload["error"]


def test_duplicate_scene_id_conflicts(client: TestClient, tmp_path: Path) -> None:
    scene_id = build_scene(client, tmp_path, scene_id="scene_dup")
    response = client.post(
        "/scenes", json={"clips_dir": str(tmp_path), "goal": "x", "scene_id": scene_id}
    )
    assert response.status_code == 409


def test_unknown_scene_404(client: TestClient) -> None:
    assert client.get("/scenes/ghost").status_code == 404


def test_queue_and_run_bassito_jobs(client: TestClient, tmp_path: Path) -> None:
    scene_id = build_scene(client, tmp_path)

    bridge = client.post(
        f"/scenes/{scene_id}/jobs",
        json={"job_type": "bridge_shot", "prompt": "night sky bridge"},
    )
    assert bridge.status_code == 202
    assert bridge.json()["job"]["job_type"] == "bridge_shot"

    extend = client.post(
        f"/scenes/{scene_id}/jobs",
        json={"job_type": "extend", "prompt": "2 more seconds", "clip_id": "clip_a"},
    )
    assert extend.status_code == 202
    assert extend.json()["queued_jobs"] == 2

    run = client.post(f"/scenes/{scene_id}/jobs/run")
    assert run.status_code == 202
    payload = wait_status(client, scene_id, until={"completed", "failed"})
    assert payload["status"] == "completed"
    assert payload["queued_bassito_jobs"] == []
    assert len(payload["bassito_history"]) == 2
    assert payload["timeline_version"] == 2


def test_extend_requires_known_clip(client: TestClient, tmp_path: Path) -> None:
    scene_id = build_scene(client, tmp_path)
    missing_clip_id = client.post(
        f"/scenes/{scene_id}/jobs", json={"job_type": "extend", "prompt": "x"}
    )
    assert missing_clip_id.status_code == 400
    unknown_clip = client.post(
        f"/scenes/{scene_id}/jobs",
        json={"job_type": "restyle", "prompt": "x", "clip_id": "ghost"},
    )
    assert unknown_clip.status_code == 404


def test_run_jobs_requires_queue(client: TestClient, tmp_path: Path) -> None:
    scene_id = build_scene(client, tmp_path)
    assert client.post(f"/scenes/{scene_id}/jobs/run").status_code == 400


def test_export_updates_profile_and_artifacts(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    scene_id = build_scene(client, tmp_path)

    rendered = tmp_path / f"{scene_id}.final.mp4"

    def fake_render(state, *, preview=False):  # noqa: ANN001
        rendered.write_bytes(b"mp4")
        return rendered

    app_registry = client.app.state.registry
    monkeypatch.setattr(app_registry.agent.tools, "render_scene_media", fake_render)

    response = client.post(
        f"/scenes/{scene_id}/export",
        json={"resolution": "1080x1080", "fps": 25, "format": "mp4"},
    )
    assert response.status_code == 202
    payload = wait_status(client, scene_id, until={"completed", "failed"})
    assert payload["status"] == "completed"
    assert payload["artifacts"]["video_path"] == str(rendered)

    video = client.get(f"/scenes/{scene_id}/video")
    assert video.status_code == 200
    assert video.content == b"mp4"


def test_video_404_when_not_rendered(client: TestClient, tmp_path: Path) -> None:
    scene_id = build_scene(client, tmp_path)
    # video_path points at a file the stub never wrote
    assert client.get(f"/scenes/{scene_id}/video").status_code == 404


def test_bearer_auth(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PINOCUT_API_TOKEN", "secret")
    client = TestClient(create_app(StubAgent(tmp_path)))
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/health", headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200


def test_openapi_schema_exposes_engine_tools(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    engine_paths = (
        "/scenes",
        "/scenes/{scene_id}",
        "/scenes/{scene_id}/jobs",
        "/scenes/{scene_id}/export",
    )
    for path in engine_paths:
        assert path in paths


def test_cli_serve_without_fastapi(monkeypatch, capsys) -> None:
    import builtins

    from pinocut import cli

    real_import = builtins.__import__

    def block_api(name, *args, **kwargs):  # noqa: ANN001
        if name == "pinocut.director_api":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_api)
    assert cli.main(["serve"]) == 1
    assert "api" in capsys.readouterr().err
