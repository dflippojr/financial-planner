"""Check or extract a receipts archive without trusting its member names.

Only regular files and directories with relative names are accepted. An
absolute path, a ".." component, a link or a device refuses the whole archive
before anything is written. Messages are fixed: member names can be receipt
file names, so they are never printed.

    extract_receipts.py check ARCHIVE
    extract_receipts.py extract ARCHIVE DEST
"""

import sys
import tarfile
from pathlib import PurePosixPath


class UnsafeArchive(Exception):
    pass


def safe_members(archive):
    members = []
    for member in archive.getmembers():
        path = PurePosixPath(member.name.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise UnsafeArchive
        if not (member.isfile() or member.isdir()):
            raise UnsafeArchive
        members.append(member)
    return members


def main(argv):
    if len(argv) not in (3, 4) or argv[1] not in ("check", "extract") or (argv[1] == "extract") != (len(argv) == 4):
        print("Usage: extract_receipts.py check ARCHIVE | extract ARCHIVE DEST", file=sys.stderr)
        return 2
    try:
        with tarfile.open(argv[2], "r:gz") as archive:
            members = safe_members(archive)
            if argv[1] == "extract":
                if hasattr(tarfile, "data_filter"):
                    archive.extractall(argv[3], members=members, filter="data")
                else:
                    archive.extractall(argv[3], members=members)
    except UnsafeArchive:
        print("Receipts archive refused: it has an absolute path, a .. component or a link", file=sys.stderr)
        return 1
    except (OSError, tarfile.TarError):
        print("Receipts archive could not be read", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
