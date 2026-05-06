"""
run_dashboard.py — Launcher for the AI Team dashboard.

Usage:
    python dashboard/run_dashboard.py [--port PORT] [--debug]

This starts the Flask server that serves the real-time monitoring dashboard.
By default, listens on http://localhost:5000

Configuration:
    - Port and other settings can be modified in config.json under [dashboard] section
"""

import sys
import argparse
from pathlib import Path

# Add scripts to path for config loading
scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from shared.config import load_config
from app import main


def run():
    """Parse arguments and start the dashboard."""
    parser = argparse.ArgumentParser(description="AI Team Dashboard Server")
    parser.add_argument("--port", type=int, default=None, help="Port to listen on (default: from config or 5000)")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    
    args = parser.parse_args()
    
    # Load config
    try:
        config = load_config()
        dashboard_config = config._config.get("dashboard", {})
        port = args.port or dashboard_config.get("port", 5000)
        debug = args.debug or dashboard_config.get("debug", False)
    except Exception as e:
        print(f"Warning: Could not load config: {e}")
        print("Using defaults: port=5000, debug=False")
        port = args.port or 5000
        debug = args.debug or False
    
    # Run dashboard
    main(port=port, debug=debug)


if __name__ == "__main__":
    run()
