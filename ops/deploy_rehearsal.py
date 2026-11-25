#!/usr/bin/env python3
"""Migrate the newest verified nightly dump in a throwaway stack before a deploy that adds migrations.

Stdlib only, Python 3.10+, Linux only. The port of ops/deploy-rehearsal.ps1
(#789); the .ps1 stays until the owner's first real deploy (#613). Run it from
the checkout on the host before every deploy whose release adds a migration, as
docs/deployment.md describes. It:

1. starts a Docker daemon of its own: rootless dockerd in a pinned
   docker:dind-rootless container whose only host directory is the run's work
   directory. Every compose command for the candidate goes to that daemon's
   socket, never to the host's daemon, so whatever the candidate's compose
   file holds cannot reach production's containers, volumes or files;
2. builds the candidate revision's images there under a throwaway Compose
   project (fp-rehearsal-*) with its own volumes, port, dummy secrets and a
   temporary backup directory;
3. restores a copy of the newest signed nightly dump (or --dump-path) into that
   project's empty database with the candidate's restore.sh;
4. starts the candidate's app, which runs `migrate --noinput` as production
   does, and compares every table's row count before and after;
5. starts the rest of the stack, takes one backup of the migrated database,
   then runs `reconcile_balances --json` and ops/post-deploy-check.py against it;
6. always tears the throwaway project down, then removes the isolated daemon
   with its data, so no container, volume or image of the run remains.

It prints PASS or FAIL with table names, counts and states only, never a row,
a log line or a secret, and exits 0 only on PASS. It never starts the host's
Docker; when that is not reachable it fails.

    git fetch origin
    python3 ops/deploy_rehearsal.py --candidate origin/main --config-path "$Config"

    # Synthetic only: a dump built by scripts/build-synthetic-dump.sh.
    python3 ops/deploy_rehearsal.py --candidate HEAD --dump-path .tmp/synthetic.dump \\
        --expect-count-change finance_alert,finance_aiconversation,finance_aiconversationmessage
"""
import argparse
import contextlib
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

PRODUCTION_PROJECT = "financial-planner"
PROJECT_PREFIX = "fp-rehearsal-"
# Tables Django's own bookkeeping adds to whenever a release adds a migration or a model.
GROW_ONLY_TABLES = ("django_migrations", "django_content_type", "auth_permission")
# What the documented deploy builds; config-check runs background's image.
BUILT_SERVICES = ("app", "backup", "background")
# Compose lets the environment override --env-file, so an exported production
# setting (POSTGRES_VOLUME_NAME, BACKUP_DIR, COMPOSE_FILE...) could point the
# throwaway project at real storage. Docker gets only these.
DOCKER_ENV_NAMES = ("PATH", "HOME", "TMPDIR", "XDG_RUNTIME_DIR")
DOCKER_ENV_PREFIXES = ("DOCKER_", "BUILDKIT_")
# The isolated daemon: rootless dockerd as the image's uid 1000, pinned by
# digest. Not --privileged: it needs the host's rootlesskit AppArmor profile
# (shipped with Ubuntu) to create its user namespace, /dev/net/tun for
# slirp4netns, and seccomp and /proc unmasked so it can mount /proc for its
# containers. Host networking lets the app's 127.0.0.1 port reach the host for
# the post-deploy check; the containers themselves sit behind slirp4netns with
# the host's loopback disabled.
ISOLATED_IMAGE = "docker:29-dind-rootless@sha256:ae083cc7de51e7a8e5a597f8fb774ca7914c408a95fc0f1477826a913479518a"
ISOLATED_START_SECONDS = 120
# The daemon's socket, inside the isolated container's own filesystem. The
# docker and compose clients for the candidate run there too (docker exec), so
# no host process connects to a path a candidate could replace, and every file
# compose reads (build contexts, Dockerfiles) resolves in a filesystem that
# holds nothing of the host's but the work directory.
ISOLATED_SOCKET = "unix:///home/rootless/docker.sock"
OPS_DIR = Path(__file__).resolve().parent
# The checkout the candidate revision is read from, and this script's own helpers.
REPO_ROOT = OPS_DIR.parent
SCRIPTS_DIR = OPS_DIR.parent / "scripts"

# What the resolved compose config may hold. disposable_storage_problems
# refuses every other key, so a key a later candidate needs is judged and added
# here first rather than passing because nothing looked at it.
KNOWN_TOP_LEVEL_KEYS = ("name", "services", "volumes", "networks")
KNOWN_SERVICE_KEYS = (
    "build", "command", "cpus", "depends_on", "entrypoint", "environment", "expose", "extra_hosts",
    "healthcheck", "image", "init", "ipc", "mem_limit", "mem_reservation", "network_mode", "networks", "pid", "ports",
    "pull_policy", "restart", "security_opt", "stop_grace_period", "stop_signal", "tmpfs", "user", "volumes", "working_dir",
)
# Keys a service could reach the host or another container's storage with,
# named so the refusal says why.
REFUSED_SERVICE_KEYS = {
    "volumes_from": "inherits another container's mounts",
    "devices": "maps host devices into the container",
    "privileged": "gives the container the host's devices",
    "cap_add": "adds kernel capabilities",
    "userns_mode": "shares the host's user namespace",
    "extends": "pulls in a service the guard has not read",
    "env_file": "reads a host file into the container's environment",
}
KNOWN_VOLUME_KEYS = ("name", "driver", "driver_opts", "external")
KNOWN_NETWORK_KEYS = ("name", "driver", "ipam")
KNOWN_BUILD_KEYS = ("context", "dockerfile", "args")
ALLOWED_SECURITY_OPTIONS = ("no-new-privileges", "no-new-privileges:true")
# Per mount type, the keys a mount and its type's options may set.
KNOWN_MOUNT_KEYS = {
    "bind": ("type", "source", "target", "read_only", "bind"),
    "volume": ("type", "source", "target", "read_only", "volume"),
    "tmpfs": ("type", "target", "read_only", "tmpfs"),
}
KNOWN_MOUNT_OPTIONS = {
    "bind": ("create_host_path",),
    "volume": ("nocopy", "subpath"),
    "tmpfs": ("size", "mode"),
}

Result = namedtuple("Result", "exit_code output")
ContainerState = namedtuple("ContainerState", "id status restarts health")
CountRow = namedtuple("CountRow", "table before after state failed")


class RehearsalError(Exception):
    """A fixed diagnostic written in this script; any other error is reported by type only."""


def output_lines(stdout):
    return [line for line in re.split(r"\r?\n", stdout) if line]


def project_name_problem(name):
    """Why `name` may not be torn down with `down --volumes`, or None when it may."""
    if not name:
        return "the project name is empty"
    if name.casefold() == PRODUCTION_PROJECT:
        return f"refusing the production project {PRODUCTION_PROJECT}"
    if not name.startswith(PROJECT_PREFIX):
        return f"project {name} lacks the {PROJECT_PREFIX} prefix"
    # Compose's own rule for project names, so the name compose uses is this one.
    if not re.fullmatch(r"fp-rehearsal-[a-z0-9][a-z0-9_-]*", name):
        return f"project {name} is not a valid lowercase compose project name"
    return None


def assert_project_name(name):
    reason = project_name_problem(name)
    if reason:
        raise RehearsalError(reason)


def parse_row_counts(lines):
    """row-counts.sh prints "table<TAB>count" lines."""
    counts = {}
    for line in lines:
        if not line.strip():
            continue
        match = re.fullmatch(r"([A-Za-z0-9_]+)\t([0-9]+)\s*", line)
        if not match:
            raise RehearsalError("row counts: unexpected output from row-counts.sh")
        counts[match[1]] = int(match[2])
    return counts


def compare_row_counts(before, after, expected=()):
    """One row per table in either snapshot: same, grew (bookkeeping), new, expected,
    changed or dropped. Changed and dropped fail unless the table is expected."""
    declared = {name.casefold() for name in expected}
    rows = []
    for table in sorted(set(before) | set(after)):
        old = before.get(table)
        new = after.get(table)
        if table not in before:
            state = "new"
        elif table not in after:
            state = "expected" if table.casefold() in declared else "dropped"
        elif old == new:
            state = "same"
        elif table in GROW_ONLY_TABLES and new > old:
            state = "grew"
        elif table.casefold() in declared:
            state = "expected"
        else:
            state = "changed"
        rows.append(CountRow(table, old, new, state, state in ("changed", "dropped")))
    return rows


def format_count_row(row):
    def show(count):
        return "-" if count is None else str(count)

    prefix = "FAIL" if row.failed else "ok  "
    return f"{prefix} counts: {row.table:<48} {show(row.before):>9} -> {show(row.after):<9} {row.state}"


def format_verdict(problems, sha, tables, migrations):
    short = sha[:12] if sha else "unresolved candidate"
    if problems > 0:
        return f"FAIL deploy rehearsal of {short}: {problems} problem(s); do not deploy"
    return f"PASS deploy rehearsal of {short}: {tables} tables compared, {migrations} migration(s) applied"


def split_names(values):
    """`--expect-count-change a,b c` and a repeated flag both give [a, b, c]."""
    return [name.strip() for value in values for name in value.split(",") if name.strip()]


def prop(value, name):
    """A key of a JSON object, or None when the value is not an object or lacks it."""
    return value.get(name) if isinstance(value, dict) else None


def prop_names(value):
    return list(value) if isinstance(value, dict) else []


def text(value):
    """The value as compose would print it in a refusal: nothing for null."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(text(item) for item in value)
    return str(value)


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def new_work_dir():
    """A new, empty directory only this user can enter, named at random rather than
    after --project-name. mkdtemp creates it exclusively with mode 0700, so a path
    someone else created first, or a symlink they planted there, is never reused."""
    return tempfile.mkdtemp(prefix="fp-rehearsal-work-")


def directory_identity(path):
    """What identifies the directory at `path` itself, not where a link there points:
    device and inode. Someone who can write its parent could rename it and put a
    link to real backups in its place; teardown compares this with the value
    pinned at creation before it empties or removes anything. The check can still
    lose a race with a swap between it and Docker opening the bind source: a work
    directory under a parent another account can write, without the sticky bit, is
    outside what this script defends."""
    try:
        status = os.lstat(path)
    except (OSError, ValueError):
        return None
    if not stat.S_ISDIR(status.st_mode):
        return None
    return f"{status.st_dev}:{status.st_ino}"


def canonical_path(path):
    """`path` with every symlink along it followed, as Docker follows them in a bind
    source, or None when it cannot be resolved. Parts that do not exist yet are
    kept as written: compose creates them as plain directories. A part that
    cannot be examined (no permission, say) makes the whole path unresolvable."""
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    pending = [part for part in reversed(path.split("/")) if part]
    current = "/"
    links = 0
    missing = False
    try:
        while pending:
            part = pending.pop()
            if part == ".":
                continue
            if part == "..":
                if missing:
                    return None
                current = os.path.dirname(current)
                continue
            following = os.path.join(current, part)
            if not missing:
                try:
                    mode = os.lstat(following).st_mode
                except (FileNotFoundError, NotADirectoryError):
                    mode = None
                if mode is None:
                    missing = True
                elif stat.S_ISLNK(mode):
                    links += 1
                    if links > 40:
                        return None
                    target = os.readlink(following)
                    if target.startswith("/"):
                        current = "/"
                    pending.extend(piece for piece in reversed(target.split("/")) if piece)
                    continue
            current = following
    except (OSError, ValueError):
        return None
    return current


def under_directory(path, root):
    """Whether `path`, links followed, is `root` or lies under it. False when either
    cannot be resolved."""
    resolved_root = canonical_path(root)
    resolved = canonical_path(path)
    if not resolved_root or not resolved:
        return False
    return (resolved.rstrip("/") + "/").startswith(resolved_root.rstrip("/") + "/")


def candidate_file_problem(path, root):
    """Why the script may not read `path` from the extracted candidate `root`, or
    None when it may. git archive keeps a committed symlink, so the candidate's
    compose.yml could point at a compose file elsewhere on the host, whose inline
    settings would then pass the allowlist unseen. No part of the path from `root`
    down may be a link, and it must be a regular file that resolves under `root`."""
    path = os.fspath(path)
    root = os.fspath(root)
    relative = os.path.relpath(path, root)
    current = root
    for part in (".", *relative.split("/")):
        current = os.path.normpath(os.path.join(current, part))
        if os.path.islink(current):
            shown = os.path.relpath(current, root)
            if shown == relative:
                return f"{relative} is a symbolic link, which the guard does not follow"
            return f"{relative}: {shown} is a symbolic link, which the guard does not follow"
    if relative.startswith("..") or not os.path.isfile(path) or not under_directory(path, root):
        return f"{relative} is not a regular file in the candidate source"
    return None


def in_project(name, project):
    return name.startswith((f"{project}-", f"{project}_"))


def service_mount_problems(service, mounts, declared_volumes, work_dir):
    """Problems with one service's mounts. A bind's source, links followed, must be
    under the work directory, read-only or not: a read-only bind of the Docker
    socket still drives the daemon. A volume mount must name a project volume
    declared at the top level, which disposable_storage_problems checks; an
    anonymous one is refused. tmpfs is memory only."""
    problems = []
    for mount in as_list(mounts):
        if mount is None:
            continue
        kind = text(prop(mount, "type"))
        if kind not in KNOWN_MOUNT_KEYS:
            problems.append(f"service {service} volumes: mount type '{kind}' is not bind, volume or tmpfs")
            continue
        for key in prop_names(mount):
            if key not in KNOWN_MOUNT_KEYS[kind]:
                problems.append(f"service {service} volumes: a {kind} mount sets {key}")
        options = prop(mount, kind)
        if options is not None and not isinstance(options, dict):
            problems.append(f"service {service} volumes: a {kind} mount sets {kind} to something other than a mapping")
        for key in prop_names(options):
            if key not in KNOWN_MOUNT_OPTIONS[kind]:
                problems.append(f"service {service} volumes: a {kind} mount sets {kind}.{key}")
        source = text(prop(mount, "source"))
        if kind == "bind" and not under_directory(source, work_dir):
            problems.append(f"service {service} volumes: bind {source} is outside the work directory")
        elif kind == "volume" and source not in declared_volumes:
            problems.append(f"service {service} volumes: volume '{source}' is not a declared project volume")
    return problems


def _mapping_problems(config):
    """compose prints these as mappings; any other shape is one the guard cannot read."""
    problems = []
    for key in ("volumes", "networks", "services"):
        value = prop(config, key)
        if value is not None and not isinstance(value, dict):
            problems.append(f"top-level {key}: not a mapping the guard can read")
    return problems


def _volume_problems(volumes, project, existing_volumes):
    problems = []
    for key, volume in volumes.items():
        name = text(prop(volume, "name"))
        if not in_project(name, project):
            problems.append(f"volume {key} is named {name}")
        if prop(volume, "external") not in (None, False):
            problems.append(f"volume {key} is external")
        driver = prop(volume, "driver")
        if driver is not None and driver != "local":
            problems.append(f"volume {key} uses driver {text(driver)}")
        if prop(volume, "driver_opts") not in (None, {}):
            problems.append(f"volume {key} sets driver_opts")
        for option in prop_names(volume):
            if option not in KNOWN_VOLUME_KEYS:
                problems.append(f"volume {key} sets {option}")
        if name and name in existing_volumes:
            problems.append(f"volume {key} ({name}) already exists")
    return problems


def _network_problems(networks, project):
    problems = []
    for key, network in networks.items():
        name = text(prop(network, "name"))
        if not in_project(name, project):
            problems.append(f"network {key} is named {name}")
        driver = prop(network, "driver")
        if driver is not None and driver != "bridge":
            problems.append(f"network {key} uses driver {text(driver)}")
        if prop(network, "ipam") not in (None, {}):
            problems.append(f"network {key} sets ipam")
        for option in prop_names(network):
            if option not in KNOWN_NETWORK_KEYS:
                problems.append(f"network {key} sets {option}")
    return problems


def _build_problems(name, spec, project, work_dir):
    """A host file the candidate names is read before any container runs: a build's
    context and Dockerfile, links followed, must be under the work directory."""
    build = prop(spec, "build")
    if build is None:
        return []
    problems = [f"service {name} build: sets {key}" for key in prop_names(build) if key not in KNOWN_BUILD_KEYS]
    context = text(prop(build, "context"))
    if not under_directory(context, work_dir):
        problems.append(f"service {name} build: context {context} is outside the work directory")
    # The builder reads the Dockerfile wherever it is: compose keeps the
    # path as written, relative to the context unless it is absolute.
    dockerfile = text(prop(build, "dockerfile")) or "Dockerfile"
    if not under_directory(os.path.join(context, dockerfile), work_dir):
        problems.append(f"service {name} build: dockerfile {dockerfile} is outside the work directory")
    image = prop(spec, "image")
    if image is not None and not text(image).startswith(f"{project}-"):
        problems.append(f"service {name} image: {text(image)} is not a {project}- image")
    return problems


def _service_problems(name, spec, project, work_dir, declared_volumes, declared_networks):
    if spec is not None and not isinstance(spec, dict):
        return [f"service {name}: not a mapping the guard can read"]
    problems = []
    for key in prop_names(spec):
        if key in REFUSED_SERVICE_KEYS:
            problems.append(f"service {name} {key}: {REFUSED_SERVICE_KEYS[key]}")
        elif key not in KNOWN_SERVICE_KEYS:
            problems.append(f"service {name} {key}: not a key the guard knows")
    for key in ("pid", "ipc", "network_mode"):
        mode = text(prop(spec, key))
        if mode.casefold() == "host" or mode.startswith("container:"):
            problems.append(f"service {name} {key}: {mode} shares a namespace outside the project")
    for option in as_list(prop(spec, "security_opt")):
        if option is not None and option not in ALLOWED_SECURITY_OPTIONS:
            problems.append(f"service {name} security_opt: {text(option)} is not allowed")
    attached = prop(spec, "networks")
    if attached is not None and not isinstance(attached, dict):
        problems.append(f"service {name} networks: not a mapping the guard can read")
    for network, options in (attached.items() if isinstance(attached, dict) else ()):
        if network not in declared_networks:
            problems.append(f"service {name} networks: {network} is not a project network")
        if options is not None:
            problems.append(f"service {name} networks: {network} sets attachment options")
    problems += _build_problems(name, spec, project, work_dir)
    problems += service_mount_problems(name, prop(spec, "volumes"), declared_volumes, work_dir)
    return problems


def disposable_storage_problems(config, project, work_dir, existing_volumes=()):
    """Problems with the resolved compose config, as an allowlist: only the keys
    listed above, and of storage only new local project volumes, binds under the
    work directory and tmpfs. `down --volumes` deletes what compose resolves,
    restore replaces /receipts and teardown empties the backup tree, so anything
    else could reach another stack's storage: volumes_from inherits a production
    container's mounts, and a host namespace, device or capability reaches the
    host itself. A prefixed volume name proves nothing about where the data
    lives: an external volume, another driver or driver_opts (type=none, o=bind,
    device=...) can put real storage behind it, and a volume that exists already
    may be one, so only a new local volume without options counts as disposable.
    Built images must carry the project's name, so a build never retags a
    production image that teardown would then leave behind. env_file is refused
    outright."""
    if not isinstance(config, dict):
        return ["compose config: not a mapping the guard can read"]
    work_dir = os.fspath(work_dir)
    problems = [f"top-level {key}: not a key the guard knows" for key in config if key not in KNOWN_TOP_LEVEL_KEYS]
    problems += _mapping_problems(config)
    volumes = prop(config, "volumes")
    volumes = volumes if isinstance(volumes, dict) else {}
    networks = prop(config, "networks")
    networks = networks if isinstance(networks, dict) else {}
    services = prop(config, "services")
    problems += _volume_problems(volumes, project, existing_volumes)
    problems += _network_problems(networks, project)
    for name, spec in (services.items() if isinstance(services, dict) else ()):
        problems += _service_problems(name, spec, project, work_dir, list(volumes), list(networks))
    return problems


def compose_source_problems(path):
    """Compose resolves include and extends before `config` prints the model, so
    the model cannot show that the candidate's compose.yml pulled in a file the
    guard never read. Nor can it be trusted to show env_file: before Compose 5,
    `config` reads every env_file even with --no-env-resolution and drops the
    key from what it prints. compose.yml is refused if it names any of the three
    anywhere, in any case, or holds a backslash or a !, the only ways YAML spells
    a key without its letters ("\\x69nclude", or a !!binary key). YAML may also be
    UTF-16 or UTF-32, where those letters are other bytes, so anything but UTF-8
    without a NUL is refused. This runs before `config`, so no env_file is read."""
    try:
        source = Path(path).read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        source = "\0"
    if "\0" in source:
        return ["compose.yml: not UTF-8 text, where the guard could miss include, extends or env_file"]
    problems = []
    for key in ("include", "extends"):
        if re.search(key, source, re.IGNORECASE):
            problems.append(f"compose.yml {key}: pulls in compose files the guard has not read")
    if re.search("env_file", source, re.IGNORECASE):
        problems.append(f"compose.yml env_file: {REFUSED_SERVICE_KEYS['env_file']}")
    if "\\" in source:
        problems.append("compose.yml: a backslash escape could spell include, extends or env_file")
    if "!" in source:
        problems.append("compose.yml: a YAML tag could spell include, extends or env_file")
    return problems


def parse_env_value(raw):
    """Compose env-file rules, as in ops/post-deploy-check.py."""
    value = raw.strip()
    single = re.match(r"'([^']*)'", value)
    if single:
        return single[1]
    double = re.match(r'"((?:[^"\\]|\\.)*)"', value)
    if double:
        return re.sub(r'\\(["\\])', r"\1", double[1])
    return re.sub(r"\s+#.*$", "", value).strip()


def read_env_value(path, key):
    """The last assignment of `key` in the env file, or None; no other value is kept."""
    if not os.path.isfile(path):
        return None
    value = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*(?:export\s+)?" + re.escape(key) + r"\s*=(.*)$", line)
        if match:
            value = parse_env_value(match[1])
    return value


def newest_signed_dump(names):
    """Of the file names in the nightly directory, the newest dump with a manifest
    and signature beside it. restore.sh checks the signature and the digests
    before it restores anything."""
    dumps = sorted((name for name in names if re.fullmatch(r"financial_planner_[A-Za-z0-9_-]+\.dump", name)), reverse=True)
    for dump in dumps:
        stem = dump[:-len(".dump")]
        if f"{stem}.sha256" in names and f"{stem}.sha256.sig" in names:
            return dump
    return None


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def docker_environment(environ=None):
    """Only DOCKER_ENV_NAMES, DOCKER_* and BUILDKIT_* of the caller's environment."""
    environ = os.environ if environ is None else environ
    return {name: value for name, value in environ.items()
            if name in DOCKER_ENV_NAMES or name.startswith(DOCKER_ENV_PREFIXES)}


def run_docker(arguments):
    """Runs docker with only docker_environment(). Output is returned, never
    printed; stderr is dropped because restore and migrate errors can quote row
    values."""
    try:
        done = subprocess.run(  # noqa: S603 - an argv list of this script's own arguments, no shell
            ["docker", *arguments],  # noqa: S607 - the operator's docker, found on PATH as the documented deploy finds it
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=docker_environment(),
            encoding="utf-8", errors="replace", check=False)
    except OSError:
        return Result(-1, [])
    return Result(done.returncode, output_lines(done.stdout))


def isolated_arguments(isolated_id, arguments):
    """The host command that runs `docker <arguments>` inside the isolated container."""
    return ["exec", "-e", f"DOCKER_HOST={ISOLATED_SOCKET}", isolated_id, "docker", *arguments]


@contextlib.contextmanager
def isolated_docker_path(isolated_id):
    """Yields a PATH whose first `docker` runs each command inside the isolated
    container, for the helpers this script starts (post-deploy-check.py,
    prod_signals.py), then removes it. The shim lives in a new directory of its
    own, never in the work directory the candidate can write."""
    if not isolated_id:
        raise RehearsalError("the isolated daemon is not running, so nothing may talk to the candidate's project")
    real = shutil.which("docker")
    if not real:
        raise RehearsalError("docker is not on PATH")
    command = " ".join(shlex.quote(part) for part in [os.path.abspath(real), *isolated_arguments(isolated_id, [])])
    bin_dir = tempfile.mkdtemp(prefix="fp-rehearsal-bin-")
    try:
        descriptor = os.open(os.path.join(bin_dir, "docker"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(descriptor, "w", encoding="utf-8") as shim:
            shim.write(f'#!/bin/sh\nexec {command} "$@"\n')
        yield bin_dir + os.pathsep + os.environ.get("PATH", "")
    finally:
        shutil.rmtree(bin_dir, ignore_errors=True)


def run_backup_helper(mounts, command):
    """Runs the pinned image as root on the host's Docker, with no network and
    `mounts` read-only except the destination, for the two steps that need to
    read root-only backups. --mount fails on a missing source rather than
    creating it."""
    arguments = ["run", "--rm", "--network", "none", "--user", "0:0", "--entrypoint", "sh"]
    for mount in mounts:
        arguments += ["--mount", mount]
    return run_docker([*arguments, ISOLATED_IMAGE, "-c", *command])


def nightly_names(nightly):
    """The file names in the nightly directory, which the backup scheduler's umask
    leaves readable by root alone."""
    listed = run_backup_helper([f"type=bind,source={nightly},target=/nightly,readonly"], ["ls -1A /nightly"])
    if listed.exit_code != 0:
        raise RehearsalError("cannot list BACKUP_DIR/nightly")
    return listed.output


def copy_nightly_dump(nightly, dump, public_key, destination):
    """Copies the chosen dump, its receipts archive, manifest and signature, and
    the public signing key into the work directory, owned by the operator, so
    the isolated daemon needs no host directory but that one."""
    stem = dump[:-len(".dump")]
    owner = f"{os.getuid()}:{os.getgid()}"
    copy = run_backup_helper(
        [f"type=bind,source={nightly},target=/nightly,readonly",
         f"type=bind,source={public_key},target=/signing.pub,readonly", f"type=bind,source={destination},target=/input"],
        ['for f in /nightly/"$1".*; do cp -- "$f" /input/; done && cp /signing.pub /input/backup-signing-key.pub '
         '&& chown -R "$2" /input', "copy", stem, owner])
    if copy.exit_code != 0:
        raise RehearsalError("copying the nightly dump into the work directory failed")


def project_status(name):
    """The status `docker compose ls` gives a project, such as running(5), or absent."""
    listed = run_docker(["compose", "ls", "--all", "--format", "json"])
    if listed.exit_code != 0:
        raise RehearsalError("docker compose ls failed")
    try:
        projects = json.loads("\n".join(listed.output)) if listed.output else []
    except ValueError:
        raise RehearsalError("docker compose ls failed") from None
    for project in as_list(projects):
        if text(prop(project, "Name")).casefold() == name.casefold():
            return text(prop(project, "Status"))
    return "absent"


def write_env_file(path, project, backup_dir, port, sha):
    """Dummy values only. The schedules name a time that is never near a
    rehearsal, and the harness, chat and notices are off, so the restored copy
    never syncs, calls out or sends anything. Encrypted fields stay sealed:
    this is not the production FIELD_ENCRYPTION_KEY."""
    lines = [
        "POSTGRES_PASSWORD=rehearsal-only-not-a-secret",
        "DJANGO_SECRET_KEY=rehearsal-only-not-a-secret",
        "DJANGO_ALLOWED_HOSTS=rehearsal.example.test,127.0.0.1,localhost",
        "DJANGO_CSRF_TRUSTED_ORIGINS=https://rehearsal.example.test",
        "FIELD_ENCRYPTION_KEY=cmVoZWFyc2FsLW9ubHktbm90LWEtcmVhbC1rZXktMzI=",
        f"BACKUP_DIR={backup_dir}",
        f"APP_PORT={port}",
        f"GIT_SHA={sha}",
        f"POSTGRES_VOLUME_NAME={project}-postgres-data",
        f"RECEIPTS_VOLUME_NAME={project}-receipts",
        "SIMPLEFIN_SYNC_CRON='0 4 1 1 *'",
        "BACKUP_CRON='0 4 1 1 *'",
        "AGENT_HARNESS_ALLOWED_ORIGINS=",
        "AI_CHAT_LOCAL_ENABLED=false",
        "OPERATOR_USERNAMES=",
    ]
    Path(path).write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def _git(*arguments):
    try:
        return subprocess.run(  # noqa: S603 - an argv list of fixed git commands, no shell
            ["git", "-C", str(REPO_ROOT), *arguments],  # noqa: S607 - the operator's git, found on PATH
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="replace", check=False)
    except OSError:
        return None


def resolve_candidate(revision):
    done = _git("rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{commit}}")
    sha = done.stdout.strip() if done and done.returncode == 0 else ""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise RehearsalError(f"cannot resolve --candidate {revision} to a commit in this checkout; run git fetch origin")
    return sha


def extract_candidate(sha, work_dir, source):
    """The candidate's tree as git archive writes it: committed files only."""
    archive = os.path.join(work_dir, "source.tar")
    done = _git("archive", "--format=tar", "-o", archive, sha)
    if not done or done.returncode != 0:
        raise RehearsalError("git archive of the candidate failed")
    try:
        code = subprocess.run(  # noqa: S603 - an argv list of this script's own paths, no shell
            ["tar", "-xf", archive, "-C", source],  # noqa: S607 - the host's tar, found on PATH
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode
    except OSError:
        code = -1
    if code != 0:
        raise RehearsalError("extracting the candidate source failed")
    os.unlink(archive)


class Rehearsal:
    """One run: what it has created so far, and so what teardown has to remove."""

    def __init__(self, args=None):
        self.args = parse_arguments([]) if args is None else args
        self.failures = []
        self.project = None
        self.sha = None
        self.armed = False
        self.isolated_id = None
        self.isolated_volume_owned = False
        self.built = False
        self.work_dir = None
        self.work_dir_identity = None
        self.compose_file = None
        self.env_file = None
        self.production_before = None
        self.tables = 0
        self.migrations = 0

    def fail(self, reason):
        self.failures.append(reason)
        print(f"FAIL {reason}", flush=True)

    @staticmethod
    def ok(message):
        print(f"ok   {message}", flush=True)

    def work_dir_unchanged(self):
        """Whether the work directory is still the directory this run created."""
        if not self.work_dir or not self.work_dir_identity:
            return False
        return directory_identity(self.work_dir) == self.work_dir_identity

    def isolated(self, arguments):
        """Runs docker inside the isolated container against its own daemon. There is
        no fallback: until this run has created that container, nothing may talk to
        the candidate's project."""
        if not self.isolated_id:
            raise RehearsalError("the isolated daemon is not running, so nothing may talk to the candidate's project")
        return run_docker(isolated_arguments(self.isolated_id, arguments))

    def compose(self, arguments):
        """Every compose command for the candidate runs in the isolated container, so a
        service the allowlist never saw (one in an inactive profile, say) is built
        and run only there, from files only there."""
        return self.isolated(["compose", "-p", self.project, "-f", self.compose_file, "--env-file", self.env_file,
                              *arguments])

    def run_helper(self, arguments):
        """Runs one of this checkout's Python helpers with the isolated `docker` first
        on PATH and, like docker itself, only docker_environment(). Its stderr is
        dropped, as docker's is."""
        with isolated_docker_path(self.isolated_id) as path:
            try:
                done = subprocess.run(  # noqa: S603 - this interpreter and this checkout's own scripts, no shell
                    [sys.executable, *arguments], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, env={**docker_environment(), "PATH": path}, encoding="utf-8", errors="replace",
                    check=False)
            except OSError:
                return Result(-1, [])
        return Result(done.returncode, output_lines(done.stdout))

    def isolated_names(self):
        return f"{self.project}-isolated", f"{self.project}-isolated-docker"

    def start_isolated_daemon(self):
        """Starts the isolated daemon on the host's Docker with the script's own fixed
        arguments: the pinned image, a new data volume, and the work directory at its
        own path (compose names bind sources by their host paths). Nothing from the
        candidate reaches this command. A container or volume of that name refuses
        the run rather than being reused or, later, removed: it may be another
        rehearsal's under the same --project-name. Teardown removes only what this run
        created. It waits until the daemon answers and reports itself rootless."""
        assert_project_name(self.project)
        container, volume = self.isolated_names()
        for listing in (["container", "ls", "-a", "-q", "--filter", f"name=^/{container}$"],
                        ["volume", "ls", "-q", "--filter", f"name=^{volume}$"]):
            found = run_docker(listing)
            if found.exit_code != 0:
                raise RehearsalError("cannot list containers and volumes on the host's Docker")
            if found.output:
                raise RehearsalError(
                    f"{container} or its volume already exists; another rehearsal may be running as {self.project}")
        created = run_docker([
            "create", "--name", container,
            "--network", "host",
            "--security-opt", "apparmor=rootlesskit", "--security-opt", "seccomp=unconfined",
            "--security-opt", "systempaths=unconfined", "--device", "/dev/net/tun",
            "-v", f"{volume}:/home/rootless/.local/share/docker",
            "-v", f"{self.work_dir}:{self.work_dir}",
            ISOLATED_IMAGE, "dockerd", f"--host={ISOLATED_SOCKET}"])
        container_id = created.output[-1] if created.output else ""
        if created.exit_code != 0 or not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise RehearsalError("creating the isolated daemon failed")
        self.isolated_id = container_id
        self.isolated_volume_owned = True
        if run_docker(["start", self.isolated_id]).exit_code != 0:
            raise RehearsalError("the isolated daemon did not start; docs/deployment.md lists what it needs")
        deadline = time.monotonic() + ISOLATED_START_SECONDS
        while True:
            info = self.isolated(["info", "--format", "{{range .SecurityOptions}}{{println .}}{{end}}"])
            if info.exit_code == 0:
                if "name=rootless" not in info.output:
                    raise RehearsalError("the isolated daemon is not rootless")
                return
            if time.monotonic() >= deadline:
                raise RehearsalError(
                    f"the isolated daemon did not answer within {ISOLATED_START_SECONDS} seconds "
                    "(it runs as uid 1000 and needs the work directory)")
            time.sleep(2)

    def stop_isolated_daemon(self):
        """Removes the isolated container this run created, and with it every
        container, image and volume inside, then its data volume, and confirms on
        the host's Docker that neither remains. A failed removal or a listing that
        cannot be read is a failure, never taken for "gone"."""
        assert_project_name(self.project)
        container, volume = self.isolated_names()
        before = len(self.failures)
        if self.isolated_id and run_docker(["rm", "--force", "--volumes", self.isolated_id]).exit_code != 0:
            self.fail(f"teardown: could not remove the isolated daemon {container}")
        if self.isolated_volume_owned and run_docker(["volume", "rm", "--force", volume]).exit_code != 0:
            self.fail(f"teardown: could not remove the isolated daemon's volume {volume}")
        checks = []
        if self.isolated_id:
            checks.append(["container", "ls", "-a", "-q", "--filter", f"id={self.isolated_id}"])
        if self.isolated_volume_owned:
            checks.append(["volume", "ls", "-q", "--filter", f"name=^{volume}$"])
        for listing in checks:
            left = run_docker(listing)
            if left.exit_code != 0:
                self.fail(f"teardown: cannot confirm that the isolated daemon {container} and its volume are gone")
            elif left.output:
                self.fail(f"teardown: the isolated daemon {container} or its volume remains")
        if len(self.failures) == before:
            self.ok(f"teardown: the isolated daemon is gone, and every container, volume and image of {self.project} with it")

    def container_state(self, service):
        ids = self.compose(["ps", "--all", "-q", service])
        container_id = ids.output[0] if ids.output else None
        if not container_id:
            return ContainerState(None, "absent", 0, "none")
        inspect = self.isolated([
            "inspect", "--format",
            "{{.State.Status}} {{.RestartCount}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
            container_id])
        parts = inspect.output[0].split(" ") if inspect.output else []
        if inspect.exit_code != 0 or len(parts) != 3 or not re.fullmatch(r"[0-9]+", parts[1]):
            return ContainerState(container_id, "unknown", 0, "none")
        return ContainerState(container_id, parts[0], int(parts[1]), parts[2])

    def wait_app_healthy(self, timeout_seconds):
        """Healthy, or why not: a stopped or restarting container is final, since
        start-production.sh exits when migrate fails and Docker starts it again."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            state = self.container_state("app")
            if state.status != "running" or state.restarts > 0 or state.health == "healthy":
                return state
            if time.monotonic() >= deadline:
                return state
            time.sleep(3)

    def print_traceback_types(self, container_id, since):
        """Exception type names and counts from the app's log, never a log line."""
        result = self.run_helper([
            str(SCRIPTS_DIR / "prod_signals.py"), "tracebacks", "--container", container_id,
            "--since", since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")])
        if result.exit_code != 0 or not result.output:
            return
        try:
            counts = json.loads("\n".join(result.output))
            total = int(prop(counts, "tracebacks") or 0)
        except (ValueError, TypeError):
            return
        if total > 0:
            types = ", ".join(f"{name} x{count}" for name, count in (prop(counts, "types") or {}).items())
            print(f"     app logged {total} traceback(s): {types}", flush=True)

    def row_counts(self):
        result = self.compose(["run", "--rm", "-T", "backup", "/opt/financial-planner/row-counts.sh"])
        if result.exit_code != 0:
            raise RehearsalError("row counts: row-counts.sh failed")
        return parse_row_counts(result.output)

    def choose_dump(self):
        """The dump to restore and what to call it. Reads nothing through Docker."""
        args = self.args
        if args.dump_path:
            if not os.path.isfile(args.dump_path):
                raise RehearsalError("--dump-path is not a file")
            return os.path.abspath(args.dump_path), None
        backup_dir = read_env_value(args.config_path, "BACKUP_DIR")
        if not backup_dir:
            raise RehearsalError("--config-path has no BACKUP_DIR")
        return None, backup_dir

    def rehearse(self):
        args = self.args
        if not args.candidate:
            raise RehearsalError("pass --candidate, the git revision to rehearse")
        if args.dump_path and args.config_path:
            raise RehearsalError("pass --dump-path or --config-path, not both")
        if not args.dump_path and not args.config_path:
            raise RehearsalError(
                "pass --config-path to rehearse the newest nightly dump, or --dump-path for a synthetic one")
        self.project = args.project_name or PROJECT_PREFIX + uuid.uuid4().hex[:8]
        assert_project_name(self.project)
        self.sha = resolve_candidate(args.candidate)
        dump, backup_dir = self.choose_dump()
        dump_kind = "synthetic dump (no signed manifest)"

        if run_docker(["info", "--format", "{{.ServerVersion}}"]).exit_code != 0:
            raise RehearsalError("Docker is not reachable; start it yourself, this script never does")
        nightly = public_key = None
        if not args.dump_path:
            # The nightly directory is root-only, so a helper container lists it.
            nightly = os.path.join(backup_dir, "nightly")
            dump = newest_signed_dump(nightly_names(nightly))
            if not dump:
                raise RehearsalError("no signed nightly dump in BACKUP_DIR/nightly; run a backup first")
            public_key = os.path.join(backup_dir, "signing/backup-signing-key.pub")
            dump_kind = f"nightly {dump}"
        self.production_before = project_status(PRODUCTION_PROJECT)
        self.ok(f"production: project {PRODUCTION_PROJECT} is {self.production_before}")
        if project_status(self.project) != "absent":
            raise RehearsalError(f"project {self.project} already exists; remove it or pass another --project-name")

        port = args.app_port if args.app_port > 0 else free_port()
        self.work_dir = new_work_dir()
        self.work_dir_identity = directory_identity(self.work_dir)
        if not self.work_dir_identity:
            raise RehearsalError("cannot read the identity of the new work directory")
        source = os.path.join(self.work_dir, "source")
        backups = os.path.join(self.work_dir, "backups")
        inputs = os.path.join(self.work_dir, "input")
        for directory in (source, backups, inputs):
            os.mkdir(directory)
        extract_candidate(self.sha, self.work_dir, source)
        self.compose_file = os.path.join(source, "compose.yml")
        self.env_file = os.path.join(self.work_dir, "rehearsal.env")
        write_env_file(self.env_file, self.project, backups, port, self.sha)
        self.ok(f"candidate: {self.sha[:12]} as project {self.project} on 127.0.0.1:{port}")

        # -f names this file alone, so no override or COMPOSE_FILE adds another, and
        # --env-file stops compose reading the candidate's .env: compose.yml is the
        # only candidate file read before teardown is armed.
        linked = candidate_file_problem(self.compose_file, source)
        if linked:
            raise RehearsalError(f"compose file the guard cannot vouch for: {linked}")
        problems = compose_source_problems(self.compose_file)
        if problems:
            raise RehearsalError(f"compose file the guard cannot vouch for: {'; '.join(problems)}")

        # The dump is copied in, so the isolated daemon gets no host directory but
        # the work directory. From here on every compose command goes to that
        # daemon; the host's Docker only starts and removes it.
        name = os.path.basename(dump)
        if args.dump_path:
            shutil.copyfile(dump, os.path.join(inputs, name))
        else:
            copy_nightly_dump(nightly, name, public_key, inputs)
        self.start_isolated_daemon()
        self.ok(f"isolated: rootless daemon {self.isolated_names()[0]}; compose for the candidate runs inside it, "
                "never against the host's Docker")

        # The allowlist below stays as a second line: it judges the model compose
        # prints, which leaves out a service in an inactive profile, so it is not
        # what keeps the candidate away from production. The isolated daemon is.
        # Without --no-env-resolution compose reads each service's env_file and
        # folds it into environment, dropping the key the guard refuses. Compose
        # before 5 does that regardless, which is why compose_source_problems
        # refuses env_file in the text first.
        config = self.compose(["config", "--no-env-resolution", "--format", "json"])
        if config.exit_code != 0:
            raise RehearsalError("docker compose config failed for the candidate")
        try:
            model = json.loads("\n".join(config.output))
        except ValueError:
            raise RehearsalError("docker compose config failed for the candidate") from None
        # Absence from `compose ls` does not mean its volumes are new. One that
        # exists is refused, not reused, and never armed for `down --volumes`.
        existing = self.isolated(["volume", "ls", "-q"])
        if existing.exit_code != 0:
            raise RehearsalError("docker volume ls failed")
        problems = disposable_storage_problems(model, self.project, self.work_dir, existing.output)
        if problems:
            raise RehearsalError(f"storage outside the throwaway project: {'; '.join(problems)}")
        self.ok(f"storage: only new {self.project}-* volumes, bind mounts under the work directory and tmpfs")

        # From here the project may hold containers, volumes and images; teardown removes them.
        self.armed = True
        if self.compose(["build", *BUILT_SERVICES]).exit_code != 0:
            raise RehearsalError("building the candidate images failed")
        self.built = True
        self.ok(f"build: {', '.join(BUILT_SERVICES)}")
        if self.compose(["up", "-d", "--wait", "--wait-timeout", "180", "db"]).exit_code != 0:
            raise RehearsalError("db did not report healthy")

        restore = ["run", "--rm", "-T"]
        if args.dump_path:
            restore += ["-v", f"{os.path.join(inputs, name)}:/rehearsal/{name}:ro", "-e", "RESTORE_ALLOW_UNVERIFIED=1"]
        else:
            restore += ["-v", f"{inputs}:/rehearsal:ro",
                        "-v", f"{os.path.join(inputs, 'backup-signing-key.pub')}:/rehearsal-signing.pub:ro",
                        "-e", "BACKUP_SIGNING_PUBLIC_KEY=/rehearsal-signing.pub"]
        restore += ["backup", "/opt/financial-planner/restore.sh", f"/rehearsal/{name}"]
        if self.compose(restore).exit_code != 0:
            raise RehearsalError(f"restore.sh refused or failed on the {dump_kind}")
        before = self.row_counts()
        if not before:
            raise RehearsalError("the restored database has no tables")
        self.ok(f"restore: {dump_kind}, {len(before)} tables")

        deploy_start = datetime.now(timezone.utc)
        if self.compose(["up", "-d", "app"]).exit_code != 0:
            raise RehearsalError("starting the candidate app failed")
        app = self.wait_app_healthy(args.migrate_timeout_seconds)
        if app.health != "healthy" or app.restarts > 0:
            self.fail(f"migrate: app is {app.status}, health {app.health}, RestartCount {app.restarts} after migrating")
            if app.id:
                self.print_traceback_types(app.id, deploy_start)
            return
        after = self.row_counts()
        rows = compare_row_counts(before, after, args.expect_count_change)
        for row in rows:
            if row.failed:
                self.failures.append(f"counts: {row.table} {row.state}")
            print(format_count_row(row), flush=True)
        self.tables = len(rows)
        self.migrations = after.get("django_migrations", 0) - before.get("django_migrations", 0)
        self.ok(f"migrate: app healthy, {self.migrations} migration(s) applied")

        if self.compose(["up", "-d"]).exit_code != 0:
            self.fail("up: the full stack did not start (config-check or background)")
            return
        if self.compose(["run", "--rm", "-T", "backup", "/opt/financial-planner/backup.sh"]).exit_code != 0:
            self.fail("backup: backup.sh failed on the migrated database")
        else:
            self.ok("backup: the migrated database dumps and restores")
        self.reconcile()
        self.post_deploy_check(port, backups, deploy_start)

    def reconcile(self):
        reconcile = self.compose(["exec", "-T", "app", "python", "manage.py", "reconcile_balances", "--json"])
        summary = None
        if reconcile.exit_code in (0, 1):
            try:
                summary = json.loads("\n".join(reconcile.output))
                mismatches = int(prop(prop(summary, "counts"), "mismatch") or 0)
            except (ValueError, TypeError):
                summary = None
        counts = prop(summary, "counts")
        if not isinstance(counts, dict):
            self.fail(f"reconcile: reconcile_balances --json exited {reconcile.exit_code} without a summary")
            return
        parts = ", ".join(f"{text(count)} {name}" for name, count in counts.items())
        accounts = text(prop(summary, "accounts"))
        known = self.args.known_mismatch_windows
        if mismatches > known:
            self.fail(f"reconcile: {accounts} account(s): {parts}; more than --known-mismatch-windows {known}")
        else:
            self.ok(f"reconcile: {accounts} account(s): {parts}")

    def post_deploy_check(self, port, backups, deploy_start):
        """ops/post-deploy-check.py against the throwaway project: its docker is the
        isolated daemon's, and only its own output lines are shown."""
        result = self.run_helper([
            str(OPS_DIR / "post-deploy-check.py"),
            "--project-name", self.project,
            "--health-url", f"http://127.0.0.1:{port}/health/",
            "--expected-sha", self.sha,
            "--since", deploy_start.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "--stable-seconds", str(self.args.stable_seconds),
            "--status-path", os.path.join(backups, "health/status")])
        for line in result.output:
            print(f"     post-deploy {line}", flush=True)
        if result.exit_code != 0:
            self.fail(f"post-deploy: the check exited {result.exit_code}")
        else:
            self.ok("post-deploy: passed")

    def teardown(self):
        assert_project_name(self.project)
        if self.compose(["down", "--volumes", "--remove-orphans", "--timeout", "10"]).exit_code != 0:
            self.fail(f"teardown: docker compose down failed for {self.project}")
        backup_image = f"{self.project}-backup"
        if self.built and self.isolated(["image", "inspect", backup_image]).exit_code == 0:
            # The containers write the backup tree as root. Once they are gone, empty
            # it from a plain container (compose would recreate the receipts volume)
            # so no dump or audit file outlives the rehearsal.
            backups = os.path.join(self.work_dir, "backups")
            if not self.work_dir_unchanged():
                self.fail("teardown: the work directory was replaced after it was created; left it unemptied")
            elif under_directory(backups, self.work_dir):
                self.isolated(["run", "--rm", "--network", "none", "-v", f"{backups}:/backups", "--entrypoint", "sh",
                               backup_image, "-c", "find /backups -mindepth 1 -delete"])
            else:
                self.fail("teardown: the backup directory no longer resolves inside the work directory; left it unemptied")
        # Its images, volumes and any container left go with the isolated daemon.

    def remove_work_dir(self):
        if not self.work_dir or not os.path.lexists(self.work_dir):
            return
        if not self.work_dir_unchanged():
            self.fail(f"teardown: the work directory {self.work_dir} was replaced after it was created; left it in place")
            return
        try:
            shutil.rmtree(self.work_dir)
        except OSError:
            self.fail(f"teardown: could not remove the work directory {self.work_dir}")

    def guarded(self, label, step):
        """Messages raised above are this script's own; anything else by type only.
        Ctrl+C stops the step, not the teardown after it."""
        try:
            step()
        except RehearsalError as error:
            self.fail(f"{label}: {error}")
        except KeyboardInterrupt:
            self.fail(f"{label}: interrupted")
        except Exception as error:  # noqa: BLE001 - reported by type; teardown must still run
            self.fail(f"{label}: {type(error).__name__}")

    def check_production(self):
        if self.production_before is None:
            return
        try:
            after = project_status(PRODUCTION_PROJECT)
        except RehearsalError:
            after = "unreadable"
        if after == self.production_before:
            self.ok(f"production: project {PRODUCTION_PROJECT} is still {after}")
        else:
            self.fail(f"production: project {PRODUCTION_PROJECT} went from {self.production_before} to {after} "
                      "during the rehearsal")

    def run(self):
        self.guarded("rehearsal", self.rehearse)
        if self.armed:
            self.guarded("teardown", self.teardown)
        if self.isolated_id or self.isolated_volume_owned:
            self.guarded("teardown", self.stop_isolated_daemon)
        self.guarded("teardown", self.remove_work_dir)
        self.guarded("production", self.check_production)
        print(format_verdict(len(self.failures), self.sha, self.tables, self.migrations), flush=True)
        return 1 if self.failures else 0


def parser():
    result = argparse.ArgumentParser(
        description="Migrate the newest verified nightly dump in a throwaway stack before a deploy that adds migrations.")
    result.add_argument("--candidate", help="a git revision in this checkout: a SHA, a tag or a branch such as origin/main")
    result.add_argument("--config-path", help="the production env file; only BACKUP_DIR is read from it, to find the "
                        "newest signed dump in BACKUP_DIR/nightly, and no other value reaches the throwaway stack")
    result.add_argument("--dump-path", help="a synthetic dump to restore instead, restored without a signed manifest")
    result.add_argument("--project-name", help="defaults to fp-rehearsal-<random>; it must carry the fp-rehearsal- prefix")
    result.add_argument("--app-port", type=int, default=0, help="loopback port for the throwaway app; 0 picks a free one")
    result.add_argument("--expect-count-change", nargs="*", action="extend", default=[], metavar="TABLE",
                        help="tables whose row count the release's migrations change on purpose, as its release notes "
                        "or tests/test_migrate_populated.py declare, separated by commas or spaces; any other change fails")
    result.add_argument("--known-mismatch-windows", type=int, default=0, help="mismatched reconciliation windows "
                        "production already shows (Settings, Data); more than this fails")
    result.add_argument("--migrate-timeout-seconds", type=int, default=900,
                        help="how long the migrated app has to report healthy")
    result.add_argument("--stable-seconds", type=int, default=60,
                        help="passed to the post-deploy check: how long every container must stay up")
    return result


def parse_arguments(argv=None):
    args = parser().parse_args(argv)
    args.expect_count_change = split_names(args.expect_count_change)
    return args


def main(argv=None):
    return Rehearsal(parse_arguments(argv)).run()


if __name__ == "__main__":
    sys.exit(main())
