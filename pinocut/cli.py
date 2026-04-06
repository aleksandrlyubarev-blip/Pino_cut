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
    parser.add_argument("--no-parallel", action="store_true", help="Disable parallel analysis (default: parallel on)")
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


def main(argv: list[str] | None = None) -> int:
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
        parallel=not args.no_parallel,
        sandbox=args.sandbox,
        memory_persist=args.memory_persist,
        metrics_enabled=args.metrics,
        metrics_path=args.metrics_path,
    )

    # Optional: register in Andrew Swarm
    swarm_registry = None
    if args.register_swarm:
        from pinocut.integrations import AgentCard, SwarmRegistry
        swarm_registry = SwarmRegistry()
        swarm_registry.register(AgentCard())

    agent = PinoCutAgent(structured_logs=args.structured_logs, swarm_registry=swarm_registry)
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
