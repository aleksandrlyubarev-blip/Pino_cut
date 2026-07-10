#!/usr/bin/env python3
"""PinoCut Resolve template 1: project bootstrap.

Creates (or opens) a Resolve project, sets timeline format, and builds a
media-pool bin structure mirroring the PinoCut project folder tree
(docs/director/project-structure.md).

Requires DaVinci Resolve running (Studio for external scripting; the free
version only runs scripts from its internal console). Tested API surface:
Resolve 18/19/20 Scripting API.

Usage:
    python resolve_bootstrap.py --name "2026-07 RoboQC Heatmap" --fps 25 \
        --width 1080 --height 1080
"""

from __future__ import annotations

import argparse
import os
import sys

# Bin layout mirrors 02_assets/ so imports stay 1:1 with the filesystem.
BIN_TREE = [
    "01_docs",
    "02_assets/gen",
    "02_assets/gen/_character_sheets",
    "02_assets/footage",
    "02_assets/screen",
    "02_assets/audio/vo",
    "02_assets/audio/music",
    "02_assets/audio/sfx",
    "02_assets/brand",
    "03_timelines",
]


def get_resolve():
    """Locate the DaVinciResolveScript module across platforms."""
    try:
        import DaVinciResolveScript as dvr  # already on PYTHONPATH
    except ImportError:
        candidates = {
            "win32": r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules",
            "darwin": "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules",
            "linux": "/opt/resolve/Developer/Scripting/Modules",
        }
        path = os.environ.get("RESOLVE_SCRIPT_API_MODULES") or candidates.get(sys.platform)
        if not path or not os.path.isdir(path):
            sys.exit(
                "DaVinciResolveScript module not found. Set RESOLVE_SCRIPT_API_MODULES "
                "to Resolve's Developer/Scripting/Modules directory."
            )
        sys.path.append(path)
        import DaVinciResolveScript as dvr

    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        sys.exit(
            "Could not connect to Resolve. Is it running, and is external scripting "
            "enabled? (Preferences > System > General > External scripting using: Local)"
        )
    return resolve


def make_bins(media_pool, root_folder, paths: list[str]) -> None:
    """Create nested bins under the media pool root, idempotently."""
    for path in paths:
        parent = root_folder
        for segment in path.split("/"):
            existing = {f.GetName(): f for f in parent.GetSubFolderList()}
            parent = existing.get(segment) or media_pool.AddSubFolder(parent, segment)
            if parent is None:
                sys.exit(f"Failed to create bin '{segment}' in '{path}'")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", required=True, help="Resolve project name")
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    args = ap.parse_args()

    resolve = get_resolve()
    pm = resolve.GetProjectManager()

    project = pm.LoadProject(args.name) or pm.CreateProject(args.name)
    if project is None:
        sys.exit(f"Could not create or open project '{args.name}'")

    # Timeline format must be set before the first timeline is created;
    # SetSetting returns False (not an exception) on invalid values.
    settings = {
        "timelineFrameRate": str(args.fps),
        "timelineResolutionWidth": str(args.width),
        "timelineResolutionHeight": str(args.height),
    }
    for key, value in settings.items():
        if not project.SetSetting(key, value):
            print(f"WARNING: Resolve rejected {key}={value} (project may have media already)")

    media_pool = project.GetMediaPool()
    make_bins(media_pool, media_pool.GetRootFolder(), BIN_TREE)

    pm.SaveProject()
    print(f"OK: project '{args.name}' ready — {args.width}x{args.height}@{args.fps}, {len(BIN_TREE)} bins")


if __name__ == "__main__":
    main()
