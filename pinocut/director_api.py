"""HTTP wrapper exposing the PinoCut Engine to the Director layer.

Maps the ``engine`` tool category of ``docs/director/tools.json`` onto the
existing :class:`~pinocut.scene_stitcher.SceneStitcherAgent`, so an LLM
runtime (Custom GPT Actions, Assistants API, Claude tool use) can drive scene
assembly over HTTP. FastAPI serves the OpenAPI schema at ``/openapi.json`` —
that document is what a Custom GPT Action imports.

Design constraints mirrored from the engine:
- Scene builds, Bassito rounds, and renders are long-running: mutating
  endpoints return ``202 Accepted`` immediately and the client polls
  ``GET /scenes/{scene_id}``. LLM action runtimes time out in ~45 s, so
  nothing heavy runs on the request thread.
- Work is serialized on a single worker (one render/GPU at a time), the same
  policy as Bassito's JobQueue.
- Auth: set ``PINOCUT_API_TOKEN`` to require ``Authorization: Bearer <token>``;
  unset means open (local development only).

Run with ``pinocut serve`` (requires the ``api`` extra: ``pip install -e .[api]``).
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from pinocut.scene_stitcher import SceneBuildRequest, SceneStitcherAgent
from pinocut.state import ExportProfile, SceneState
from pinocut.utils.logging import StageLogger

ACTIVE_STATUSES = {"building", "rebuilding", "rendering"}

_bearer = HTTPBearer(auto_error=False)


def require_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    token = os.environ.get("PINOCUT_API_TOKEN", "")
    if not token:
        return
    if credentials is None or credentials.credentials != token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
        )


# --------------------------------------------------------------------------
# Request / response models (these become the Actions schema — keep described)
# --------------------------------------------------------------------------

class SceneBuildBody(BaseModel):
    """Parameters for the Director's `scene_build` tool."""

    clips_dir: str = Field(
        description="Folder with source video clips, on the engine host"
    )
    goal: str = Field(
        description="Scene goal, e.g. 'tense night-time reveal of the prototype'"
    )
    style: str = Field(default="cinematic", description="Style profile name")
    template: Literal["dialogue_scene", "cinematic_montage", "trailer_cut"] = (
        "cinematic_montage"
    )
    target_duration_sec: float = Field(default=30.0, gt=0)
    scene_id: str | None = Field(
        default=None, description="Defaults to scene_<NN> assigned by the server"
    )
    project_id: str = "rfv_pinnocat_001"
    output_dir: str = Field(default="./output", description="Directory for scene artifacts")
    editing_mode: Literal["manual", "hybrid", "auto"] = "hybrid"
    max_clips: int = Field(default=10, ge=1)
    music_path: str | None = None
    voiceover_path: str | None = None
    resolution: str = "1920x1080"
    fps: int = 24
    run_bassito: bool = Field(
        default=False,
        description=(
            "Execute queued Bassito jobs (extend/restyle/bridge) right after "
            "the build and rebuild the scene"
        ),
    )


class BassitoJobBody(BaseModel):
    """Queue a generative job on a built scene.

    Director tools request_bridge_shot / request_extend / request_restyle.
    """

    job_type: Literal["bridge_shot", "extend", "restyle"]
    prompt: str = Field(description="Generation prompt for the missing/updated material")
    clip_id: str | None = Field(
        default=None,
        description="Source clip id (filename stem); required for extend and restyle",
    )
    params: dict[str, Any] = Field(default_factory=dict)


class ExportBody(BaseModel):
    """Full-quality export with a new profile (Director tool export_scene)."""

    resolution: str = "1920x1080"
    fps: int = 24
    format: str = "mp4"


# --------------------------------------------------------------------------
# Scene registry
# --------------------------------------------------------------------------

@dataclass
class SceneRecord:
    scene_id: str
    status: str = "queued"  # queued | building | rebuilding | rendering | completed | failed
    state: SceneState | None = None
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class SceneRegistry:
    """In-memory scene store with one serialized worker for heavy operations."""

    def __init__(self, agent: SceneStitcherAgent):
        self.agent = agent
        self.records: dict[str, SceneRecord] = {}
        self._registry_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="pinocut-engine"
        )
        self._counter = 0
        self.log = StageLogger("director_api")

    def new_record(self, scene_id: str | None) -> SceneRecord:
        with self._registry_lock:
            if scene_id is None:
                self._counter += 1
                scene_id = f"scene_{self._counter:02d}"
            if scene_id in self.records:
                raise HTTPException(
                    status_code=409, detail=f"Scene '{scene_id}' already exists"
                )
            record = SceneRecord(scene_id=scene_id)
            self.records[scene_id] = record
            return record

    def get(self, scene_id: str) -> SceneRecord:
        record = self.records.get(scene_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Scene '{scene_id}' not found")
        return record

    def submit(self, record: SceneRecord, busy_status: str, work) -> None:
        """Transition the record and run `work` on the serialized worker."""
        with record.lock:
            if record.status in ACTIVE_STATUSES:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Scene '{record.scene_id}' is busy ({record.status}); "
                        "poll until it completes"
                    ),
                )
            record.status = busy_status
            record.error = None

        def run() -> None:
            try:
                work()
                with record.lock:
                    record.status = "completed"
            except Exception as exc:  # surfaced via GET /scenes/{id}
                self.log.warn(f"{busy_status} failed for {record.scene_id}: {exc}")
                with record.lock:
                    record.status = "failed"
                    record.error = str(exc)

        self._executor.submit(run)


def scene_payload(record: SceneRecord) -> dict[str, Any]:
    state = record.state
    payload: dict[str, Any] = {
        "scene_id": record.scene_id,
        "status": record.status,
        "error": record.error,
    }
    if state is None:
        return payload

    queued = list(state.bridge_jobs) + list(state.regeneration_jobs)
    payload.update(
        {
            "scene_goal": state.scene_goal,
            "style_profile": state.style_profile,
            "timeline_version": state.timeline_version,
            "approved_clips": state.approved_clips,
            "rejected_clips": state.rejected_clips,
            "artifacts": {
                key: state.reviews.get(key)
                for key in (
                    "video_path",
                    "preview_video_path",
                    "timeline_path",
                    "preview_path",
                    "scene_ops_path",
                )
            },
            "queued_bassito_jobs": [job.to_dict() for job in queued],
            "bassito_history": [job.to_dict() for job in state.bassito_history],
            "warnings": [str(err) for err in state.errors],
        }
    )
    return payload


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------

def create_app(agent: SceneStitcherAgent | None = None) -> FastAPI:
    app = FastAPI(
        title="PinoCut Engine API",
        description=(
            "Engine tool endpoints for the PinoCut Director "
            "(docs/director/tools.json, category 'engine')."
        ),
        version="1.0.0",
        dependencies=[Depends(require_token)],
    )
    registry = SceneRegistry(agent or SceneStitcherAgent())
    app.state.registry = registry

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "pinocut-engine"}

    @app.get("/scenes")
    def list_scenes() -> list[dict[str, Any]]:
        return [scene_payload(record) for record in registry.records.values()]

    @app.post("/scenes", status_code=status.HTTP_202_ACCEPTED)
    def build_scene(body: SceneBuildBody) -> dict[str, Any]:
        """Director tool `scene_build`: assemble a scene from a folder of clips."""
        clips_dir = Path(body.clips_dir)
        if not clips_dir.is_dir():
            raise HTTPException(
                status_code=400, detail=f"clips_dir '{body.clips_dir}' is not a directory"
            )
        record = registry.new_record(body.scene_id)

        request = SceneBuildRequest(
            input_folder=clips_dir,
            output_dir=Path(body.output_dir),
            project_id=body.project_id,
            scene_id=record.scene_id,
            scene_goal=body.goal,
            style_profile=body.style,
            target_duration_sec=body.target_duration_sec,
            editing_mode=body.editing_mode,
            editing_template=body.template,
            max_clips=body.max_clips,
            music_path=Path(body.music_path) if body.music_path else None,
            voiceover_path=Path(body.voiceover_path) if body.voiceover_path else None,
            export_profile=ExportProfile(resolution=body.resolution, fps=body.fps, format="mp4"),
        )

        def work() -> None:
            state = registry.agent.build_from_request(request)
            record.state = state
            fatal = [
                err
                for err in state.errors
                if getattr(err.severity, "value", err.severity) == "fatal"
            ]
            if fatal:
                raise RuntimeError("; ".join(str(err) for err in fatal))
            if body.run_bassito and (state.bridge_jobs or state.regeneration_jobs):
                record.state = registry.agent.run_bassito_round(state)

        registry.submit(record, "building", work)
        return {"scene_id": record.scene_id, "status": record.status}

    @app.get("/scenes/{scene_id}")
    def get_scene(scene_id: str) -> dict[str, Any]:
        return scene_payload(registry.get(scene_id))

    @app.post("/scenes/{scene_id}/jobs", status_code=status.HTTP_202_ACCEPTED)
    def queue_job(scene_id: str, body: BassitoJobBody) -> dict[str, Any]:
        """Queue a Bassito job (bridge_shot / extend / restyle) without executing it."""
        record = registry.get(scene_id)
        state = _require_state(record)

        if body.job_type == "bridge_shot":
            job = registry.agent.tools.request_bridge_shot(body.prompt, params=body.params)
            state.bridge_jobs.append(job)
        else:
            if not body.clip_id:
                raise HTTPException(
                    status_code=400, detail=f"clip_id is required for {body.job_type}"
                )
            clip = next(
                (c for c in state.available_clips if c.path.stem == body.clip_id), None
            )
            if clip is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Clip '{body.clip_id}' not in scene '{scene_id}'",
                )
            job = registry.agent.extend_or_restyle_clip(clip, body.job_type, body.prompt)
            job.params.update(body.params)
            state.regeneration_jobs.append(job)

        queued = len(state.bridge_jobs) + len(state.regeneration_jobs)
        return {"scene_id": scene_id, "queued_jobs": queued, "job": job.to_dict()}

    @app.post("/scenes/{scene_id}/jobs/run", status_code=status.HTTP_202_ACCEPTED)
    def run_jobs(scene_id: str) -> dict[str, Any]:
        """Execute queued Bassito jobs and rebuild the scene (regeneration loop)."""
        record = registry.get(scene_id)
        state = _require_state(record)
        if not state.bridge_jobs and not state.regeneration_jobs:
            raise HTTPException(status_code=400, detail="No queued Bassito jobs to run")

        def work() -> None:
            record.state = registry.agent.run_bassito_round(record.state)

        registry.submit(record, "rebuilding", work)
        return {"scene_id": scene_id, "status": record.status}

    @app.post("/scenes/{scene_id}/preview", status_code=status.HTTP_202_ACCEPTED)
    def render_preview(scene_id: str) -> dict[str, Any]:
        """Director tool `render_preview`: fast low-res proxy of the current timeline."""
        record = registry.get(scene_id)
        state = _require_state(record, needs_timeline=True)

        def work() -> None:
            path = registry.agent.tools.render_scene_media(state, preview=True)
            if path is None:
                raise RuntimeError("Preview render unavailable (ffmpeg or source clips missing)")
            state.reviews["preview_video_path"] = str(path)

        registry.submit(record, "rendering", work)
        return {"scene_id": scene_id, "status": record.status}

    @app.post("/scenes/{scene_id}/export", status_code=status.HTTP_202_ACCEPTED)
    def export_scene(scene_id: str, body: ExportBody) -> dict[str, Any]:
        """Director tool `export_scene`: full-quality render with an export profile."""
        record = registry.get(scene_id)
        state = _require_state(record, needs_timeline=True)

        profile = ExportProfile(resolution=body.resolution, fps=body.fps, format=body.format)

        def work() -> None:
            state.export_profile = profile
            state.timeline.export_profile = profile
            timeline_path = registry.agent.tools.export_scene(
                state.timeline, profile, state.output_dir
            )
            state.reviews["timeline_path"] = str(timeline_path)
            path = registry.agent.tools.render_scene_media(state)
            if path is None:
                raise RuntimeError("Export render unavailable (ffmpeg or source clips missing)")
            state.reviews["video_path"] = str(path)

        registry.submit(record, "rendering", work)
        return {"scene_id": scene_id, "status": record.status}

    @app.get("/scenes/{scene_id}/video")
    def download_video(scene_id: str, preview: bool = False) -> FileResponse:
        record = registry.get(scene_id)
        state = _require_state(record)
        key = "preview_video_path" if preview else "video_path"
        path = state.reviews.get(key)
        if not path or not Path(path).exists():
            raise HTTPException(status_code=404, detail=f"No rendered {key} for scene '{scene_id}'")
        return FileResponse(path, media_type="video/mp4", filename=Path(path).name)

    return app


def _require_state(record: SceneRecord, *, needs_timeline: bool = False) -> SceneState:
    if record.status in ACTIVE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Scene '{record.scene_id}' is busy ({record.status}); poll until it completes",
        )
    if record.state is None:
        raise HTTPException(
            status_code=409, detail=f"Scene '{record.scene_id}' has no built state yet"
        )
    if needs_timeline and record.state.timeline is None:
        raise HTTPException(
            status_code=409,
            detail=f"Scene '{record.scene_id}' has no timeline (build failed?)",
        )
    return record.state


def serve(host: str = "127.0.0.1", port: int = 8642) -> None:
    """Entry point for ``pinocut serve``."""
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port)
