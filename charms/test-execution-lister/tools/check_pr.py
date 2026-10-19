#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Canonical Ltd

"""Report how many spread tests the service would run for a snapd pull request.

Looks up the pull request's changed files on GitHub (with the gh CLI), checks
out the pull request's head in a temporary worktree of a local snapd checkout,
lists the tests of each system in its .github/workflows/data-fundamental-systems.json
with `spread -list`, and asks the test-execution-lister service, one system at
a time, which of those tests to run.

If the pull request's checkout has tests/utils/features/shared_test_users.py
(or --shared-users points at a copy), the changed files are first rewritten
with it, as CI does, so changes to shared test files select only the tests
that use them. It always searches the pull request's checkout.

Example:
    tools/check_pr.py 17856 --snapd ~/source/snapd --service http://10.113.43.87:8181
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SYSTEMS_FILE = ".github/workflows/data-fundamental-systems.json"
SHARED_USERS_SCRIPT = "tests/utils/features/shared_test_users.py"


def run(cmd: list[str], cwd: str | Path | None = None) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {result.stderr.strip()}")
    return result.stdout


def changed_files(repo: str, pr: int) -> dict[str, list[str]]:
    """The pull request's changed files, by modified/added/deleted."""
    pages = json.loads(run(["gh", "api", "--paginate", "--slurp", f"repos/{repo}/pulls/{pr}/files"]))
    files: dict[str, list[str]] = {"modified": [], "added": [], "deleted": []}
    for entry in (item for page in pages for item in page):
        status, path = entry["status"], entry["filename"]
        if status == "added":
            files["added"].append(path)
        elif status == "removed":
            files["deleted"].append(path)
        elif status in ("renamed", "copied"):
            # the old path goes away (only for renames) and the new one appears
            if status == "renamed":
                files["deleted"].append(entry["previous_filename"])
            files["added"].append(path)
        else:
            files["modified"].append(path)
    return files


def fundamental_systems(worktree: Path) -> list[tuple[str, str]]:
    """(backend:system, tasks) for each system in the systems file."""
    data = json.loads((worktree / SYSTEMS_FILE).read_text(encoding="utf-8"))
    systems = []
    for entry in data["include"]:
        for system in entry["systems"].split():
            systems.append((f"{entry['backend']}:{system}", entry.get("tasks", "tests/...")))
    return systems


def spread_tests(worktree: Path, systems: list[tuple[str, str]]) -> dict[str, list[str]]:
    def list_system(item: tuple[str, str]) -> tuple[str, list[str]]:
        system, tasks = item
        output = subprocess.run(
            ["spread", "-list", *(f"{system}:{task}" for task in tasks.split())],
            cwd=worktree, capture_output=True, text=True,
        ).stdout
        return system, output.split()

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(systems) or 1) as pool:
        return dict(pool.map(list_system, systems))


def call_service(service: str, changed: dict[str, list[str]], system: str, tests: list[str]) -> list[str]:
    body = json.dumps({"changed_files": changed, "backend_systems": [system], "tests": tests}).encode()
    request = urllib.request.Request(
        f"{service.rstrip('/')}/calculate-tests", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read())["tests"]
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"service returned {exc.code}: {exc.read().decode(errors='replace')}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pr", type=int, help="pull request number")
    parser.add_argument("--repo", default="canonical/snapd", help="GitHub repository (default: canonical/snapd)")
    parser.add_argument("--snapd", required=True, type=Path, help="local snapd git checkout used for the worktree")
    parser.add_argument("--service", default="http://127.0.0.1:8181", help="test-execution-lister service URL")
    parser.add_argument("--shared-users", type=Path,
                        help=f"shared_test_users.py to use instead of the pull request's {SHARED_USERS_SCRIPT}")
    parser.add_argument("--no-shared-users", action="store_true",
                        help=f"do not rewrite shared test files with {SHARED_USERS_SCRIPT}")
    parser.add_argument("--show-tests", action="store_true", help="also print the selected tests of each system")
    parser.add_argument("--json", type=Path, help="write the results to this file")
    args = parser.parse_args()

    pr_info = json.loads(run(["gh", "pr", "view", str(args.pr), "--repo", args.repo, "--json", "title,headRefOid,url"]))
    changed = changed_files(args.repo, args.pr)
    print(f"PR #{args.pr}: {pr_info['title']}\n{pr_info['url']}")
    print(f"changed files: {len(changed['modified'])} modified, {len(changed['added'])} added, "
          f"{len(changed['deleted'])} deleted")

    # the pull request's head, for its systems file, its tests and its shared test files
    run(["git", "-C", str(args.snapd), "fetch", "-q", f"https://github.com/{args.repo}.git", f"pull/{args.pr}/head"])
    head = run(["git", "-C", str(args.snapd), "rev-parse", "FETCH_HEAD"]).strip()
    with tempfile.TemporaryDirectory() as tmp:
        worktree = Path(tmp) / "snapd"
        run(["git", "-C", str(args.snapd), "worktree", "add", "-q", "--detach", str(worktree), head])
        try:
            sent = changed
            shared_users = args.shared_users or worktree / SHARED_USERS_SCRIPT
            if not args.no_shared_users and shared_users.exists():
                sent = _rewrite(shared_users, worktree, changed)
                print(f"shared test files rewritten with {shared_users}")
            elif not args.no_shared_users:
                print(f"{SHARED_USERS_SCRIPT} not in the pull request's checkout; shared test files run everything")

            systems = fundamental_systems(worktree)
            print(f"listing tests for {len(systems)} systems at {head[:10]} ...")
            lists = spread_tests(worktree, systems)
        finally:
            run(["git", "-C", str(args.snapd), "worktree", "remove", "--force", str(worktree)])

    results = []
    print(f"\n{'system':40} {'selected':>8} {'total':>6}")
    for system, _ in systems:
        tests = lists.get(system, [])
        if not tests:
            print(f"{system:40} {'-':>8} {0:>6}  (spread listed no tests)")
            results.append({"system": system, "selected": [], "total": 0})
            continue
        selected = call_service(args.service, sent, system, tests)
        print(f"{system:40} {len(selected):>8} {len(tests):>6}")
        if args.show_tests:
            print("\n".join(f"    {test}" for test in selected))
        results.append({"system": system, "selected": selected, "total": len(tests)})

    total_selected = sum(len(r["selected"]) for r in results)
    total = sum(r["total"] for r in results)
    if total:
        print(f"{'all systems':40} {total_selected:>8} {total:>6}  ({total_selected / total * 100:.1f}%)")

    if args.json:
        args.json.write_text(json.dumps({"pr": args.pr, "head": head, "changed_files": changed,
                                         "sent_changed_files": sent, "results": results}, indent=1), encoding="utf-8")


def _rewrite(script: Path, worktree: Path, changed: dict[str, list[str]]) -> dict[str, list[str]]:
    """Replace shared test files by the task.yaml of the tests that use them."""
    result = subprocess.run(
        [sys.executable, str(script), "--repo", str(worktree), "--changed-files", "-", "--rewrite"],
        input=json.dumps(changed), capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"shared_test_users.py failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


if __name__ == "__main__":
    main()
