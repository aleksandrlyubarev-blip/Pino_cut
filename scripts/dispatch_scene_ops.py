#!/usr/bin/env python3
"""
Dispatch a frontend-ready SceneOps snapshot to the RomeoFlexVision repository.

This helper replaces large inline Python blocks in CI workflows and gives
PinoCut a single, reusable way to publish scene state into RomeoFlexVision's
`scene_ops_snapshot` GitHub Actions workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_OWNER = "aleksandrlyubarev-blip"
DEFAULT_REPO = "RomeoFlexVision"
DEFAULT_EVENT_TYPE = "scene_ops_snapshot"
DEFAULT_TOKEN_ENV = "ROMEOFLEXVISION_DISPATCH_TOKEN"
DEFAULT_API_BASE = "https://api.github.com"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dispatch_scene_ops",
        description="Dispatch a SceneOps snapshot to the RomeoFlexVision repository",
    )
    parser.add_argument(
        "--snapshot",
        type=str,
        help="Path to a frontend-ready SceneOps JSON file. Use '-' to read from stdin.",
    )
    parser.add_argument(
        "--source-url",
        type=str,
        help="Public URL to a SceneOps JSON file. Sent as client_payload.source_url.",
    )
    parser.add_argument("--owner", default=DEFAULT_OWNER, help="Target GitHub owner/org")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="Target GitHub repository")
    parser.add_argument("--event-type", default=DEFAULT_EVENT_TYPE, help="repository_dispatch event type")
    parser.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV,
        help="Environment variable that stores the GitHub token",
    )
    parser.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help="GitHub API base URL",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the payload instead of sending it to GitHub",
    )
    args = parser.parse_args(argv)

    if not args.snapshot and not args.source_url:
        parser.error("one of --snapshot or --source-url is required")
    if args.snapshot and args.source_url:
        parser.error("use either --snapshot or --source-url, not both")

    return args


def load_snapshot(snapshot_path: str) -> dict[str, Any]:
    if snapshot_path == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(snapshot_path).read_text(encoding="utf-8")
    snapshot = json.loads(raw)
    validate_snapshot(snapshot)
    return snapshot


def validate_snapshot(snapshot: dict[str, Any]) -> None:
    required_top_level = {"scene", "clipScores", "andrew", "bassitoJobs", "updatedAt"}
    missing = sorted(required_top_level - snapshot.keys())
    if missing:
        raise ValueError(f"SceneOps snapshot missing required keys: {', '.join(missing)}")

    scene = snapshot.get("scene")
    if not isinstance(scene, dict):
        raise ValueError("SceneOps snapshot field 'scene' must be an object")

    required_scene_keys = {
        "sceneId",
        "sceneGoal",
        "editingTemplate",
        "targetDurationSec",
        "actualDurationSec",
        "usedClips",
        "rejectedClips",
        "queueState",
    }
    scene_missing = sorted(required_scene_keys - scene.keys())
    if scene_missing:
        raise ValueError(
            "SceneOps scene is missing required keys: " + ", ".join(scene_missing)
        )


def build_dispatch_body(
    *,
    event_type: str,
    snapshot: dict[str, Any] | None = None,
    source_url: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"event_type": event_type, "client_payload": {}}
    if snapshot is not None:
        payload["client_payload"]["snapshot"] = snapshot
    if source_url is not None:
        payload["client_payload"]["source_url"] = source_url
    return payload


def dispatch_repository_event(
    *,
    owner: str,
    repo: str,
    token: str,
    body: dict[str, Any],
    api_base: str = DEFAULT_API_BASE,
) -> int:
    endpoint = f"{api_base.rstrip('/')}/repos/{owner}/{repo}/dispatches"
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub dispatch failed: {exc.code} {detail}") from exc


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    snapshot = load_snapshot(args.snapshot) if args.snapshot else None
    body = build_dispatch_body(
        event_type=args.event_type,
        snapshot=snapshot,
        source_url=args.source_url,
    )

    if args.dry_run:
        print(json.dumps(body, indent=2))
        return 0

    token = os.getenv(args.token_env, "").strip()
    if not token:
        raise SystemExit(
            f"Environment variable {args.token_env} is required unless --dry-run is used"
        )

    status = dispatch_repository_event(
        owner=args.owner,
        repo=args.repo,
        token=token,
        body=body,
        api_base=args.api_base,
    )
    print(f"repository_dispatch sent successfully (status={status})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
