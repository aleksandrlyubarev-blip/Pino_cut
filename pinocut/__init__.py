"""PinoCut Agent — Modular AI video assembly for Andrew Swarm ecosystem.

Usage:
    from pinocut import PinoCutAgent
    from pinocut.config import ProjectConfig

    agent = PinoCutAgent()
    result = agent.run(ProjectConfig(input_folder="./footage"))
    print(agent.metrics_report())
"""

__version__ = "1.0.0"

from pinocut.agent import PinoCutAgent, build_graph
from pinocut.config import ProjectConfig

__all__ = ["PinoCutAgent", "ProjectConfig", "build_graph"]
