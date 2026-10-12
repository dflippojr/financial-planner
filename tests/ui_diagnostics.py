"""Token-free exception and operation summaries for disposable launcher failures."""
import re


def diagnostic_summary(log):
    frames = []
    for filename, line, function in re.findall(
        r'File "([^"\n]+)", line ([0-9]+), in ([A-Za-z_][A-Za-z_0-9]*)', log
    ):
        filename = filename.replace("\\", "/")
        module = re.search(r"/((?:finance|scripts|django/db)/[A-Za-z_0-9/]+\.py)$", filename)
        if module:
            frame = f"{module[1]}:{line} in {function}"
            if frame not in frames:
                frames.append(frame)
    kinds = sorted(set(re.findall(
        r"^(?:django\.db\.utils\.|sqlite3\.)?(OperationalError|IntegrityError|RuntimeError|ValueError):", log,
        flags=re.MULTILINE,
    )))
    if "database is locked" in log or "database table is locked" in log:
        kinds.append("SQLite lock")
    return "Disposable UI diagnostics: " + "; ".join(kinds + frames or ["no recognized exception frames"])
