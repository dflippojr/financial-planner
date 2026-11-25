"""A stand-in for the docker CLI that ops/deploy_rehearsal.py calls (#789).

Run as `python rehearsal_fake_docker.py CONFIG.json <docker arguments>` by the
`docker` shim tests/test_deploy_rehearsal_python.py puts first on PATH. It never
reaches a Docker daemon. Every call is appended to the config's "log" as one
JSON line holding the arguments as sent and the environment it ran with.

A command run inside the isolated container (`exec ... docker ...`) is logged
as sent and then handled as its own arguments. With "plain", nothing else
happens and the call exits "exit_code". Otherwise the stand-in acts like the
commands that touch the host:

- `create` prints "fake_id", and `info` inside the isolated container reports a
  rootless daemon.
- On the host, `container ls` and `volume ls` list the names in "on_host" their
  name filter matches; inside the isolated container `volume ls` lists
  "existing_volumes". A root helper's `ls -1A /nightly` lists "nightly".
- `compose ls` prints "compose_ls" (no projects by default).
- `compose config` goes to the real CLI at "real_docker" (it needs no daemon).
  Without that it prints a model built from "volumes", "services" and
  "top_level", plus the backup bind mount resolved from the env file's
  BACKUP_DIR as compose resolves it; {BACKUP_DIR} and {WORK} in the config
  stand for the backup and work directories.
- `compose build` exits "build_exit". With "swap_work_dir_to" it first moves
  the work directory aside and puts a link to that directory in its place.
- restore.sh empties the directory a volume's driver_opts device names, as
  replacing /receipts does on such a volume, and `run -v SRC:/backups ... find
  /backups -mindepth 1 -delete` empties SRC, following a symlink as Docker's
  mount does.
- With "create_host_binds", a `compose ... run backup` the host's Docker
  receives creates each writable bind source of that service, read from the
  full model with every profile active, as Docker would. Inside the isolated
  container those paths are its own, so the stand-in leaves the host alone.
- With "app", the app container reports that state ("running 0 healthy", say),
  row-counts.sh prints "counts_before" and, once the app has been started,
  "counts_after", `reconcile_balances` prints "reconcile" and `logs` prints
  "app_log".

Everything else succeeds silently.
"""
import json
import os
import re
import shutil
import subprocess
import sys


def _empty(source):
    for name in os.listdir(source):
        path = os.path.join(source, name)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)


def _logged(config):
    with open(config["log"], encoding="utf-8") as handle:
        return [json.loads(line)["args"] for line in handle]


def _simulated_model(config, args):
    env_file = args[args.index("--env-file") + 1]
    with open(env_file, encoding="utf-8") as handle:
        values = dict(line.split("=", 1) for line in handle.read().splitlines())
    extra = json.dumps({"services": config.get("services", {}), "top": config.get("top_level", {})})
    extra = json.loads(extra.replace("{BACKUP_DIR}", values["BACKUP_DIR"]).replace("{WORK}", os.path.dirname(env_file)))
    backup = {"volumes": [{"type": "bind", "source": values["BACKUP_DIR"], "target": "/backups"}]}
    return {"volumes": config.get("volumes", {}), "services": {"backup": backup, **extra["services"]}, **extra["top"]}


def _create_host_binds(config, args):
    model = [*args[:args.index("run")], "--profile", "*", "config", "--format", "json"]
    resolved = subprocess.run([config["real_docker"], *model], capture_output=True, text=True, check=True)
    for mount in json.loads(resolved.stdout)["services"]["backup"].get("volumes", []):
        if mount.get("type") == "bind" and not mount.get("read_only"):
            os.makedirs(mount["source"], exist_ok=True)


def _compose(config, args, isolated):
    if "ls" in args:
        print(config.get("compose_ls", "[]"))
    elif "config" in args:
        if config.get("real_docker"):
            return subprocess.run([config["real_docker"], *args], check=False).returncode
        print(json.dumps(_simulated_model(config, args)))
    elif "build" in args:
        if config.get("swap_work_dir_to"):
            work = os.path.dirname(args[args.index("--env-file") + 1])
            os.rename(work, work + "-moved")
            os.symlink(config["swap_work_dir_to"], work, target_is_directory=True)
        return config.get("build_exit", 0)
    elif any(arg.endswith("/restore.sh") for arg in args):
        for volume in config.get("volumes", {}).values():
            device = (volume.get("driver_opts") or {}).get("device")
            if device and os.path.isdir(device):
                _empty(device)
    elif "run" in args and "backup" in args and not isolated and config.get("create_host_binds"):
        _create_host_binds(config, args)
    elif any(arg.endswith("/row-counts.sh") for arg in args):
        started = any(call[-3:] == ["up", "-d", "app"] for call in _logged(config))
        counts = config.get("counts_after" if started else "counts_before", {})
        print("\n".join(f"{table}\t{count}" for table, count in counts.items()))
    elif "ps" in args and config.get("app"):
        print("a" * 64)
    elif "reconcile_balances" in args:
        print(config.get("reconcile", ""))
    return 0


def main(argv):
    with open(argv[0], encoding="utf-8") as handle:
        config = json.load(handle)
    sent = argv[1:]
    with open(config["log"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"args": sent, "env": dict(os.environ)}) + "\n")
    if config.get("plain"):
        return config.get("exit_code", 0)
    isolated = sent[:1] == ["exec"]
    args = sent[sent.index("docker") + 1:] if isolated else sent
    if args[:1] == ["create"]:
        print(config["fake_id"])
    elif args[:1] == ["info"] and isolated:
        print("name=rootless")
    elif args[:2] in (["container", "ls"], ["volume", "ls"]) and not isolated:
        pattern = next((arg[5:] for arg in args if arg.startswith("name=")), None)
        # Docker matches a container's name with its leading slash, a volume's without.
        subject = "/{}" if args[0] == "container" else "{}"
        print("\n".join(name for name in config.get("on_host", []) if pattern and re.search(pattern, subject.format(name))))
    elif args[:1] == ["run"] and "ls -1A /nightly" in args:
        print("\n".join(config.get("nightly", [])))
    elif args[:1] == ["compose"]:
        return _compose(config, args, isolated)
    elif args[:1] == ["run"] and "find /backups -mindepth 1 -delete" in args:
        _empty(next(arg.rsplit(":", 1)[0] for arg in args if arg.endswith(":/backups")))
    elif args[:2] == ["volume", "ls"]:
        print("\n".join(config.get("existing_volumes", [])))
    elif args[:2] == ["volume", "inspect"]:
        return 1
    elif args[:1] == ["inspect"] and config.get("app"):
        print(config["app"])
    elif args[:1] == ["logs"]:
        print(config.get("app_log", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
