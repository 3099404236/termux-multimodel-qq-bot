from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def main() -> None:
    module_path = (
        Path(__file__).resolve().parents[1]
        / "astrbot"
        / "core"
        / "tools"
        / "antigravity_openai_bridge.py"
    )
    spec = importlib.util.spec_from_file_location(
        "astrbot_antigravity_openai_bridge",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load bridge module from {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.main()


if __name__ == "__main__":
    main()
