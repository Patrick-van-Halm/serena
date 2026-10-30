"""Run the immutable reviewed transformation and fix pytest fixture registration."""
import subprocess
from pathlib import Path

source = subprocess.check_output([
    "git", "show", "16260f984fd3b8ccf5ecf88929b00d8a8d412050:scripts/apply_request_latency.py"
], text=True)
exec(compile(source, "reviewed_latency_changes", "exec"))
path = Path("test/serena/test_request_latency.py")
text = path.read_text(encoding="utf-8")
old = "from .test_mcp_sdk_v2 import BOOTSTRAP, isolated_project as isolated_project, text_of\n"
new = "from .test_mcp_sdk_v2 import BOOTSTRAP, isolated_project as _isolated_project, text_of\n\nisolated_project = _isolated_project\n"
assert text.count(old) == 1
path.write_text(text.replace(old, new), encoding="utf-8")
