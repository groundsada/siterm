#!/usr/bin/env python3
"""Generates the CI pylint config from the shared one.

`.github/linters/.python-lint` used to be a symlink to `standarts/pylintrc`.
Getting super-linter green needed `import-error` and `no-name-in-module`
disabled, because it runs pylint in a container with neither the project's
dependencies installed nor `src/python` on the path -- so every import is
unresolvable there. One file meant those relaxations also applied locally,
where the dependencies *are* installed and `import-error` is a real check.

pylint has no config inheritance, so the CI file has to be a complete copy.
Generating it keeps one source of truth: edit `standarts/pylintrc`, run this,
commit both. `--check` fails when the generated copy is stale, which is what
the Linter workflow runs.

    ./standarts/gen-ci-pylintrc.py            # regenerate
    ./standarts/gen-ci-pylintrc.py --check    # fail if stale
"""

import os
import sys

SHARED = "standarts/pylintrc"
GENERATED = ".github/linters/.python-lint"

# Only these two. Everything else the shared file disables is disabled because
# of how this codebase is written, not because of where pylint is running, and
# belongs in the shared file where a developer sees the same result as CI.
CONTAINER_ONLY = ["import-error", "no-name-in-module"]

HEADER = """# GENERATED FILE -- DO NOT EDIT.
#
# Produced from standarts/pylintrc by standarts/gen-ci-pylintrc.py.
# Edit the shared file and regenerate; the Linter workflow fails if this copy
# is stale.
#
# The only difference from the shared file is the two checks below, which
# super-linter cannot resolve because it runs pylint in a container without the
# project's dependencies and without src/python on the path. They fire on the
# environment rather than on the code, and they stay enabled locally.
"""


def build(shared):
    """Shared config with the container-only disables appended to `disable=`."""
    lines = shared.splitlines()
    out = []
    indisable = False
    for line in lines:
        if line.startswith("disable="):
            indisable = True
            out.append(line)
            continue
        if indisable:
            stripped = line.strip()
            # The disable list ends at the first line that is not a continuation
            # -- a blank line or the next option.
            if not stripped or (not stripped.endswith(",") and not stripped.startswith("#")):
                if stripped and not stripped.startswith("#"):
                    out.append(line.rstrip() + ",")
                else:
                    out.append(line)
                indent = " " * 8
                out.append(f"{indent}# Container-only; see the header.")
                for name in CONTAINER_ONLY:
                    suffix = "," if name != CONTAINER_ONLY[-1] else ""
                    out.append(f"{indent}{name}{suffix}")
                if not stripped:
                    out.append("")
                indisable = False
                continue
        out.append(line)
    return HEADER + "\n" + "\n".join(out).rstrip("\n") + "\n"


def main():
    """Regenerate, or check freshness."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)
    with open(SHARED, encoding="utf-8") as fd:
        want = build(fd.read())

    if "--check" in sys.argv:
        if not os.path.exists(GENERATED):
            print(f"{GENERATED} is missing. Run standarts/gen-ci-pylintrc.py")
            return 1
        with open(GENERATED, encoding="utf-8") as fd:
            if fd.read() != want:
                print(f"{GENERATED} is stale. Run standarts/gen-ci-pylintrc.py and commit the result.")
                return 1
        print(f"{GENERATED} is up to date.")
        return 0

    if os.path.islink(GENERATED):
        os.unlink(GENERATED)
    with open(GENERATED, "w", encoding="utf-8") as fd:
        fd.write(want)
    print(f"wrote {GENERATED}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
