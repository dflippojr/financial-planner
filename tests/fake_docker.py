"""A stand-in for the docker CLI that ops/post-deploy-check.ps1 calls.

It answers `ps`, `inspect` and `events` from a JSON state file named by
FAKE_DOCKER_STATE, and never reaches a real Docker daemon. A container with
"restart_on_inspect" starts again the first time it is inspected, which is a
restart after the check began but before its first snapshot saw the container.
A container is running unless it names another "status", such as "exited",
and has a RestartCount of 0 unless it names a "restart_count". It has never
stopped, so Docker's zero FinishedAt, unless it names a "finished" time.

Docker keeps a bounded event history: with "history_from" set, `events`
returns nothing older, as on a host whose other containers churn. With
"event_log" set, a restart on inspect is also appended to that file, as the
recorder docs/deployment.md starts before `up -d` would write it; the test
writes the deploy's own events there first. Events print in that recorder's
format, "<time in ns> <action> <container id>".
"""

import json
import os
import sys
from datetime import datetime, timedelta, UTC


def _docker_time(seconds):
    # Docker drops trailing zeros, so its times often have seven or fewer
    # fractional digits, which PowerShell 7's ConvertFrom-Json reads as dates.
    text = datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".")
    return text + "Z"


def _nanoseconds(seconds):
    # The same instant _docker_time writes, so a start event at a container's
    # "started" time is exactly its StartedAt, as the daemon's first event can be.
    return (datetime.fromtimestamp(seconds, UTC) - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1) * 1000


def started_at(container):
    # "started_ns" gives StartedAt all nine fractional digits Docker can write.
    if "started_ns" in container:
        seconds, fraction = divmod(container["started_ns"], 10**9)
        return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction:09d}Z"
    return _docker_time(container["started"])


def event_line(event):
    nanoseconds = event["time_ns"] if "time_ns" in event else _nanoseconds(event["time"])
    return f"{nanoseconds} {event['action']} {event['id']}"


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
        # A restart stops the container first, which sets FinishedAt.
        container["finished"] = now - 0.1
        container["started"] = now
        state["events"].append({"time": now, "action": "start", "id": container["id"]})
        if state.get("event_log"):
            with open(state["event_log"], "a", encoding="utf-8") as log:
                log.write(f"{event_line(state['events'][-1])}\n")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
    print(json.dumps([{
        "Name": "/" + container["service"],
        "Created": _docker_time(container["created"]),
        "RestartCount": container.get("restart_count", 0),
        "State": {
            "Status": container.get("status", "running"),
            "StartedAt": started_at(container),
            "FinishedAt": _docker_time(container["finished"]) if "finished" in container else "0001-01-01T00:00:00Z",
        },
        "Config": {"Labels": container.get("labels", {})},
    }]))
    return 0


def _events(state, args):
    since = float(_option_values(args, "--since")[0])
    until = float(_option_values(args, "--until")[0])
    actions = {f.split("=", 1)[1] for f in _option_values(args, "--filter") if f.startswith("event=")}
    oldest = state.get("history_from", 0)
    for event in state["events"]:
        if max(since, oldest) <= event["time"] <= until and event["action"] in actions:
            print(event_line(event))


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
