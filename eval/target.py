"""The ONLY place the eval harness knows how to reach a test target: where scenarios live and how a fault is injected / recovered.
Point it at another system with env vars, no code change:
  SRE_EVAL_DIR      directory with the target's scenario files (<name>.yaml with a `ground_truth` key). Legacy name: PLAYGROUND_DIR.
  SRE_SCENARIO_DIR  override for the scenario folder (default <SRE_EVAL_DIR>/fault-injection/scenarios)
  SRE_FAULT_CMD     command that takes `inject|recover <scenario>` (default: python <SRE_EVAL_DIR>/fault-injection/scripts/faultctl.py)
Scenario files and the fault command are ground truth: the agent never reads them."""
import os, shlex, sys
from pathlib import Path

PLAY = Path(os.getenv("SRE_EVAL_DIR") or os.getenv("PLAYGROUND_DIR") or "../playground").resolve()
SCENARIO_DIR = Path(os.getenv("SRE_SCENARIO_DIR") or PLAY / "fault-injection" / "scenarios").resolve()


def faultctl_cmd() -> list[str]:
    if os.getenv("SRE_FAULT_CMD"):
        return shlex.split(os.getenv("SRE_FAULT_CMD"))
    return [sys.executable, str(PLAY / "fault-injection" / "scripts" / "faultctl.py")]
