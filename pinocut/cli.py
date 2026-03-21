"""PinoCut CLI — command-line interface for video assembly."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pinocut.config import ColorGradeStyle, ProjectConfig, RenderPreset


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pinocut",
        description="PinoCut Agent — AI video assembly for Andrew Swarm",
    )

    parser.add_argument("input_folder", type=Path, help="Folder with source video clips")
    parser.add_argument("-o", "--output", type=Path, default=Path("./output/final.mp4"), help="Output video path")
    parser.add_argument("-m", "--music", type=Path, default=None, help="Background music file")

    # Render
    parser.add_argument("--preset", choices=["draft", "standard", "cinema"], default="standard", help="Render quality preset")
    parser.add_argument("--render-mode", choices=["moviepy", "ffmpeg"], default="moviepy", help="Render engine (default: moviepy)")

    # Clips
    parser.add_argument("--max-clips", type=int, default=30, help="Max clips to process")
    parser.add_argument("--max-duration", type=float, default=None, help="Max duration per clip (seconds)")

    # Effects
    parser.add_argument("--color-grade", choices=["none", "warm", "cold", "vintage", "cinematic"], default="none")
    parser.add_argument("--watermark", type=str, default="Romeo Flex Vision", help="Watermark text")

    # Execution
    parser.add_argument("--parallel", action="store_true", default=True, help="Parallel analysis via Moltis (default: on)")
    parser.add_argument("--no-parallel", action="store_true", help="Disable parallel analysis")
    parser.add_argument("--sandbox", choices=["local", "e2b"], default="local", help="Execution sandbox")
    parser.add_argument("--memory-persist", action="store_true", help="Persist analysis cache between runs")

    # Metrics
    parser.add_argument("--metrics", action="store_true", default=True, help="Enable pipeline metrics")
    parser.add_argument("--metrics-path", type=Path, default=None, help="JSON file for metrics history")

    # Logging
    parser.add_argument("--structured-logs", action="store_true", help="Output JSON-structured logs")

    # Andrew Swarm
    parser.add_argument("--register-swarm", action="store_true", help="Register in Andrew Swarm before running")

    return parser.parse_args(argv)


def parse_scene_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pinocut scene",
        description="Pinnocat Scene Stitcher Agent v1",
    )
    subparsers = parser.add_subparsers(dest="scene_command", required=True)

    build = subparsers.add_parser("build", help="Build one scene from source clips")
    build.add_argument("input_folder", type=Path, help="Folder with source video clips")
    build.add_argument("--output-dir", type=Path, default=Path("./output"), help="Output directory for scene artifacts")
    build.add_argument("--project-id", type=str, default="rfv_pinnocat_001")
    build.add_argument("--scene-id", type=str, default="scene_01")
    build.add_argument("--goal", type=str, required=True, help="Scene goal")
    build.add_argument("--style", type=str, default="cinematic")
    build.add_argument("--duration", type=float, default=30.0, help="Target scene duration in seconds")
    build.add_argument("--mode", choices=["manual", "hybrid", "auto"], default="hybrid")
    build.add_argument(
        "--template",
        choices=["dialogue_scene", "cinematic_montage", "trailer_cut"],
        default="cinematic_montage",
    )
    build.add_argument("--music", type=Path, default=None)
    build.add_argument("--voiceover", type=Path, default=None)
    build.add_argument("--subtitle-language", type=str, default=None)
    build.add_argument("--max-clips", type=int, default=10)
    build.add_argument("--min-duration", type=float, default=1.0)
    build.add_argument("--max-duration", type=float, default=None)
    build.add_argument("--resolution", type=str, default="1920x1080")
    build.add_argument("--fps", type=int, default=24)

    return parser.parse_args(argv)


def main_scene(argv: list[str] | None = None) -> int:
    args = parse_scene_args(argv)

    from pinocut.scene_stitcher import SceneBuildRequest, SceneStitcherAgent
    from pinocut.state import ExportProfile

    request = SceneBuildRequest(
        input_folder=args.input_folder,
        output_dir=args.output_dir,
        project_id=args.project_id,
        scene_id=args.scene_id,
        scene_goal=args.goal,
        style_profile=args.style,
        target_duration_sec=args.duration,
        editing_mode=args.mode,
        editing_template=args.template,
        max_clips=args.max_clips,
        min_duration=args.min_duration,
        max_duration=args.max_duration,
        music_path=args.music,
        voiceover_path=args.voiceover,
        subtitle_language=args.subtitle_language,
        export_profile=ExportProfile(
            resolution=args.resolution,
            fps=args.fps,
            format="mp4",
        ),
    )

    agent = SceneStitcherAgent()
    scene_state = agent.build_from_request(request)

    if scene_state.errors:
        for error in scene_state.errors:
            print(str(error), file=sys.stderr)
        if any(error.severity.value == "fatal" for error in scene_state.errors):
            return 1

    print(f"Scene: {scene_state.scene_id}")
    print(f"Approved clips: {', '.join(scene_state.approved_clips) or 'none'}")
    print(f"Rejected clips: {', '.join(scene_state.rejected_clips) or 'none'}")
    if scene_state.timeline:
        print(f"Timeline exported to: {scene_state.reviews.get('timeline_path', 'n/a')}")
        print(f"Preview manifest: {scene_state.reviews.get('preview_path', 'n/a')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    if argv and argv[0] == "scene":
        return main_scene(argv[1:])

    args = parse_args(argv)

    from pinocut.agent import PinoCutAgent
    from pinocut.config import WatermarkConfig

    config = ProjectConfig(
        input_folder=args.input_folder,
        output_path=args.output,
        bg_music_path=args.music,
        render_preset=RenderPreset(args.preset),
        render_mode=args.render_mode,
        max_clips=args.max_clips,
        max_duration=args.max_duration,
        color_grade=ColorGradeStyle(args.color_grade),
        watermark=WatermarkConfig(text=args.watermark),
        structured_logs=args.structured_logs,
        parallel=args.parallel and not args.no_parallel,
        sandbox=args.sandbox,
        memory_persist=args.memory_persist,
        metrics_enabled=args.metrics,
        metrics_path=args.metrics_path,
    )

    # Optional: register in Andrew Swarm
    if args.register_swarm:
        from pinocut.integrations import AgentCard, SwarmRegistry
        registry = SwarmRegistry()
        registry.register(AgentCard())

    agent = PinoCutAgent(structured_logs=args.structured_logs)
    result = agent.run(config)

    # Print metrics
    if config.metrics_enabled:
        print(agent.metrics_report())

    if result.get("output_path"):
        print(f"\n\u2705 Output: {result['output_path']}")
        return 0
    else:
        print("\n\u274c Pipeline failed", file=sys.stderr)
        for e in result.get("errors", []):
            print(f"  {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
