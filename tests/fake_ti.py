"""A stand-in for the ``ti`` CLI, backed by a local directory.

Mimics the contract of ``ti fs`` (flags only, JSON on stdout, raw bytes for
read-file, ``ti [ERROR]: ...`` on stderr). Environment:

- ``FAKE_TI_ROOT``: directory that plays the remote file system.
- ``FAKE_TI_LOG``: JSON-lines log of every invocation (argv and token presence).
- ``FAKE_TI_FAIL``: ``permission`` makes every command fail with exit code 4.
- ``FAKE_TI_SLEEP``: seconds to wait before doing anything (to test cancellation).
"""

import fnmatch
import json
import os
import shutil
import sys


def fail(message, code=1):
    sys.stderr.write(f"\nti [ERROR]: {message}\n")
    sys.exit(code)


def parse(argv):
    flags, i = {}, 0
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("--"):
            fail(f'unknown command "{arg}": accepts 0 arg(s)', 2)
        if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            flags.setdefault(arg[2:], []).append(argv[i + 1])
            i += 2
        else:
            flags[arg[2:]] = [True]
            i += 1
    return {k: v[-1] if k != "tag" else v for k, v in flags.items()}


def local(root, remote):
    return os.path.join(root, remote.lstrip("/"))


def out(obj):
    print(json.dumps(obj, indent=2))


def walk(root, base):
    for dirpath, _, files in os.walk(local(root, base)):
        for name in sorted(files):
            full = os.path.join(dirpath, name)
            yield "/" + os.path.relpath(full, root).replace(os.sep, "/"), name, os.path.getsize(full)


def main():
    root = os.environ["FAKE_TI_ROOT"]
    if os.environ.get("FAKE_TI_SLEEP"):
        import time

        time.sleep(float(os.environ["FAKE_TI_SLEEP"]))
    argv = sys.argv[1:]
    with open(os.environ["FAKE_TI_LOG"], "a") as log:
        log.write(json.dumps({"argv": argv, "token": os.environ.get("TI_FS_TOKEN")}) + "\n")
    if os.environ.get("FAKE_TI_FAIL") == "permission":
        fail("permission denied: token scope does not allow this operation", 4)
    if argv[:1] != ["fs"] or len(argv) < 2:
        fail("unsupported command", 2)
    cmd, f = argv[1], parse(argv[2:])

    if cmd == "read-file":
        path = local(root, f["path"])
        if not os.path.isfile(path):
            fail("fs cat: remote resource not found")
        sys.stdout.buffer.write(open(path, "rb").read())
    elif cmd == "copy-file":
        if "from-stdin" in f or "from-local" in f:
            data = sys.stdin.buffer.read() if "from-stdin" in f else open(f["from-local"], "rb").read()
            dst = local(root, f["to-remote"])
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            open(dst, "wb").write(data)
            out(
                {
                    "operation": "copy_file",
                    "source_path": f.get("from-local", "-"),
                    "target_path": f["to-remote"],
                    "status": "copied",
                }
            )
        elif "from-remote" in f and "to-local" in f:
            src, dst = local(root, f["from-remote"]), f["to-local"]
            if not os.path.exists(src):
                fail("fs cp: remote resource not found")
            if os.path.isdir(src):
                if "recursive" not in f:
                    fail("fs cp: is a directory")
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
            out({"operation": "copy_file", "source_path": f["from-remote"], "target_path": dst, "status": "copied"})
        else:
            fail("invalid copy flags", 2)
    elif cmd == "delete-file":
        path = local(root, f["path"])
        if not os.path.lexists(path):
            fail("fs rm: remote resource not found")
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        out({"operation": "delete_file", "target_path": f["path"], "status": "deleted"})
    elif cmd == "describe-file":
        path = local(root, f["path"])
        if not os.path.exists(path):
            fail("fs stat: remote resource not found")
        out({"path": f["path"], "size_bytes": os.path.getsize(path), "is_dir": os.path.isdir(path)})
    elif cmd == "list-files":
        path = local(root, f.get("path", "/"))
        if not os.path.isdir(path):
            fail("fs ls: remote resource not found")
        entries = [
            {
                "name": n,
                "size_bytes": os.path.getsize(os.path.join(path, n)),
                "is_dir": os.path.isdir(os.path.join(path, n)),
            }
            for n in sorted(os.listdir(path))
        ]
        out({"path": f.get("path", "/"), "entries": entries})
    elif cmd == "search-file-content":
        base = f.get("path", "/")
        if not os.path.isdir(local(root, base)):
            fail("fs grep: remote resource not found")
        words = f["pattern"].lower().split()
        results = [
            {"path": p, "name": n}
            for p, n, _ in walk(root, base)
            if any(w in open(local(root, p), errors="ignore").read().lower() for w in words)
        ]
        out({"path": base, "results": results[: int(f.get("limit", 0)) or None]})
    elif cmd == "find-files":
        base = f.get("path", "/")
        if not os.path.isdir(local(root, base)):
            fail("fs find: remote resource not found")
        results = [
            {"path": p, "name": n}
            for p, n, _ in walk(root, base)
            if fnmatch.fnmatch(n, f.get("file-name-pattern", "*"))
        ]
        out({"path": base, "results": results[: int(f.get("limit", 0)) or None]})
    else:
        fail(f"unsupported fake command {cmd}", 2)


if __name__ == "__main__":
    main()
