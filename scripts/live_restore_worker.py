"""Owned fixture/picker console. Never imported by the production package."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from live_restore_console import identity


def save(path, value):
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def main():
    config = Path(sys.argv[1])
    data = json.loads(config.read_text(encoding="utf-8"))
    receipt = config.with_suffix(".receipt.json")
    result = config.with_suffix(".result.json")
    save(
        receipt,
        dict(
            identity=identity(os.getpid()),
            cwd=os.getcwd(),
            wtSession=os.environ.get("WT_SESSION"),
            startedAt=time.time(),
        ),
    )
    if data["mode"] == "provider":
        process = subprocess.Popen(data["argv"])
        save(config.with_suffix(".child.json"), dict(pid=process.pid))
        code = process.wait()
    else:
        from termsync import cli, restore
        from termsync.store import Store

        def guarded_spawn(argv):
            sid = argv[argv.index("-SessionId") + 1]
            if sid not in data["ids"] or "-Run" in argv:
                raise RuntimeError("Live acceptance guard rejected unexpected launch")
            log = config.with_suffix(".launches.jsonl")
            with log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(dict(argv=argv, requestedAt=time.time())) + "\n")
            return restore.default_spawn(argv)

        # Actual console predicate, msvcrt picker, production capture/preflight,
        # launch/readiness, arm.ps1, pending and verification defaults are used.
        code = cli.restore_interactive(Store(Path(data["store"])), spawner=guarded_spawn)
    save(result, dict(exitCode=code, finishedAt=time.time()))
    print("Live validation worker finished; controller owns cleanup.", flush=True)
    while not config.with_suffix(".stop").exists():
        time.sleep(0.25)


if __name__ == "__main__":
    main()
