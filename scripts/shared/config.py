"""
config.py — Centralized configuration loader for Ollama URLs and agent models.

Usage:
    from shared.config import load_config

    config = load_config()
    ollama_url = config.ollama_base_url()
    ollama_timeout = config.ollama_timeout()
    agent_model = config.agent_model("coder")
"""

import json
from pathlib import Path
from typing import Optional, Dict, Any


class ProjectConfig:
    """Load and provide access to project configuration."""

    def __init__(self, config_dict: Dict[str, Any]):
        self._config = config_dict

    def ollama_base_url(self) -> str:
        """Get the Ollama server base URL."""
        return self._config.get("ollama", {}).get("base_url", "http://localhost:11434")

    def ollama_timeout(self) -> int:
        """Get the Ollama request timeout in seconds."""
        return self._config.get("ollama", {}).get("timeout", 120)

    def agent_model(self, agent_name: str) -> Optional[str]:
        """Get the model name for a specific agent. Returns None if agent uses external service (e.g., Claude CLI)."""
        agent_config = self._config.get("agents", {}).get(agent_name, {})
        return agent_config.get("model")

    def agent_uses_cli(self, agent_name: str) -> bool:
        """Check if agent uses external CLI (e.g., Claude) instead of Ollama."""
        agent_config = self._config.get("agents", {}).get(agent_name, {})
        return agent_config.get("cli", False)

    def agent_timeout(self, agent_name: str) -> int:
        """Get agent-specific timeout if defined, otherwise return Ollama timeout."""
        agent_config = self._config.get("agents", {}).get(agent_name, {})
        return agent_config.get("timeout", self.ollama_timeout())

    def list_agents(self) -> list[str]:
        """Get list of all configured agents."""
        return list(self._config.get("agents", {}).keys())


def load_config(config_path: Optional[Path] = None) -> ProjectConfig:
    """
    Load configuration from config.json.
    
    Args:
        config_path: Path to config.json. If None, looks for config.json in project root.
    
    Returns:
        ProjectConfig object with loaded configuration.
    
    Raises:
        FileNotFoundError: If config.json is not found.
        json.JSONDecodeError: If config.json is invalid JSON.
    """
    if config_path is None:
        # Find project root (where config.json should be)
        # config.py is at: project_root/scripts/shared/config.py
        # So we need to go up 3 levels
        script_dir = Path(__file__).resolve().parent  # scripts/shared
        project_root = script_dir.parent.parent  # project_root
        config_path = project_root / "config.json"

    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        config_dict = json.load(f)

    return ProjectConfig(config_dict)
