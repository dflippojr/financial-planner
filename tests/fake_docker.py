"""A stand-in for the docker CLI that ops/post-deploy-check.ps1 calls.

It answers `ps`, `inspect` and `events` from a JSON state file named by
FAKE_DOCKER_STATE, and never reaches a real Docker daemon. A container with
"restart_on_inspect" starts again the first time it is inspected, which is a
restart after the check began but before its first snapshot saw the container.
"""

import json
import os
import sys
from datetime import datetime, UTC


def _docker_time(seconds):
    # Docker drops trailing zeros, so its times often have seven or fewer
    # fractional digits, which PowerShell 7's ConvertFrom-Json reads as dates.
    text = datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".")
    return text + "Z"


def _option_values(args, name):
    return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == name]


def _ps(state, args):
    prefix = "label=com.docker.compose.service="
    services = [f[len(prefix):] for f in _option_values(args, "--filter") if f.startswith(prefix)]
    for container in state["containers"].values():
        if container["service"] in services:
            print(container["id"])


def _inspect(state, args, path):
    container = state["containers"].get(args[-1])
    if container is None:
        return 1
    if container.pop("restart_on_inspect", False):
        now = datetime.now(UTC).timestamp()
        container["started"] = now
        state["events"].append({"time": now, "action": "start", "id": container["id"]})
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
    print(json.dumps([{
        "Name": "/" + container["service"],
        "Created": _docker_time(container["created"]),
        "RestartCount": 0,
        "State": {"Status": "running", "StartedAt": _docker_time(container["started"])},
        "Config": {"Labels": container.get("labels", {})},
    }]))
    return 0


def _events(state, args):
    since = float(_option_values(args, "--since")[0])
    until = float(_option_values(args, "--until")[0])
    actions = {f.split("=", 1)[1] for f in _option_values(args, "--filter") if f.startswith("event=")}
    for event in state["events"]:
        if since <= event["time"] <= until and event["action"] in actions:
            print(f"{event['action']} {event['id']}")


def main(args):
    path = os.environ["FAKE_DOCKER_STATE"]
    with open(path, encoding="utf-8") as handle:
        state = json.load(handle)
    if args[:1] == ["ps"]:
        _ps(state, args)
        return 0
    if args[:1] == ["inspect"]:
        return _inspect(state, args, path)
    if args[:1] == ["events"]:
        _events(state, args)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
