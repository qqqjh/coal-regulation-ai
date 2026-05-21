import sys
import json

try:
    raw = sys.stdin.read()
    data = json.loads(raw)
    cwd = data.get("workspace", {}).get("current_dir", "")
    model = data.get("model", {}).get("display_name", "")
    remaining = data.get("context_window", {}).get("remaining_percentage")

    parts = []
    if cwd:
        parts.append(cwd)
    if model:
        parts.append(model)
    if remaining is not None:
        parts.append(f"Context: {remaining}%")

    print(" | ".join(parts))

except Exception:
    print("")
