"""Tests for the SceneOps repository_dispatch helper."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "dispatch_scene_ops.py"
)


def load_dispatch_module():
    spec = importlib.util.spec_from_file_location(
        "pinocut_dispatch_scene_ops", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_snapshot() -> dict:
    return {
        "updatedAt": "2026-03-22T09:40:00+02:00",
        "scene": {
            "sceneId": "scene_03",
            "sceneGoal": "arrival at abandoned spaceport",
            "editingTemplate": "cinematic_montage",
            "targetDurationSec": 35,
            "actualDurationSec": 32.8,
            "usedClips": ["c04", "c02", "c07", "c01", "c05"],
            "rejectedClips": ["c03", "c06", "c08"],
            "queueState": "waiting_bassito",
        },
        "clipScores": {
            "c04": {
                "visualQuality": 4,
                "continuityFit": 5,
                "promptMatch": 4,
                "motionStability": 4,
                "timelineUsefulness": 5,
                "recommendedAction": "keep",
            }
        },
        "andrew": {
            "confidence": 0.74,
            "summary": "Scene remains usable for rough-cut review.",
            "warnings": [],
            "recommendedActions": [],
            "qualityBreakdown": {
                "visual_quality": 4.33,
                "continuity_fit": 4.33,
                "prompt_match": 4.33,
                "motion_stability": 3.33,
                "timeline_usefulness": 4.33,
            },
            "hitlDecision": "skipped",
        },
        "bassitoJobs": [],
    }


def test_validate_snapshot_accepts_frontend_shape() -> None:
    dispatch = load_dispatch_module()

    dispatch.validate_snapshot(make_snapshot())


def test_validate_snapshot_rejects_missing_scene_keys() -> None:
    dispatch = load_dispatch_module()
    snapshot = make_snapshot()
    del snapshot["scene"]["queueState"]

    with pytest.raises(ValueError, match="queueState"):
        dispatch.validate_snapshot(snapshot)


def test_build_dispatch_body_supports_snapshot_and_source_url() -> None:
    dispatch = load_dispatch_module()
    snapshot = make_snapshot()

    body = dispatch.build_dispatch_body(
        event_type="scene_ops_snapshot",
        snapshot=snapshot,
        source_url="https://example.com/scene-ops.json",
    )

    assert body["event_type"] == "scene_ops_snapshot"
    assert body["client_payload"]["snapshot"] == snapshot
    assert body["client_payload"]["source_url"] == "https://example.com/scene-ops.json"


def test_main_dry_run_prints_dispatch_payload(tmp_path: Path, capsys) -> None:
    dispatch = load_dispatch_module()
    snapshot_path = tmp_path / "scene-ops.json"
    snapshot_path.write_text(json.dumps(make_snapshot()), encoding="utf-8")

    result = dispatch.main(["--snapshot", str(snapshot_path), "--dry-run"])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["event_type"] == "scene_ops_snapshot"
    assert payload["client_payload"]["snapshot"]["scene"]["sceneId"] == "scene_03"
