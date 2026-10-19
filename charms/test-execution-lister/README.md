# test-execution-lister

`test-execution-lister` is a machine charm that exposes a small local API for deciding which spread tests should run from a set of changed files, tracking manually added tests, and refreshing the test map from MongoDB through `query_features.py` from the snapd repository.

## What it does

The charm manages three pieces of state on the unit:

- a MongoDB credentials JSON file written from a Juju secret
- `spread-execution-map.json`, used to look up tests by backend and changed Go file
- `added-spread-tests.txt`, used to carry tests that should always be included

The service listens on `listen-host:listen-port` and can handle multiple requests in parallel.

## Configuration

Available config options are defined in [charmcraft.yaml](charmcraft.yaml):

- `listen-host`: host for the local API service, default `127.0.0.1`
- `listen-port`: port for the local API service, default `8181`
- `mongodb-secret-id`: Juju secret ID containing the MongoDB credentials JSON
- `snapd-repo-url`: git repository to fetch `tests/utils/features` (`query_features.py`) from, default `https://github.com/canonical/snapd.git`
- `snapd-ref`: branch, tag or full commit SHA of `snapd-repo-url` to fetch it from, default `master`
- `query-features-path`: optional path to an existing `query_features.py` on the unit; when set, nothing is fetched
- `execution-data-dir`: directory containing `spread-execution-map.json`, `interface-users.json` and `base-executions.json`
- `added-spread-tests-path`: path to the added tests text file
- `refresh-helper-command`: optional override for the helper command that rebuilds `spread-execution-map.json` and `base-executions.json`
- `base-tests-max-minutes`: maximum execution time, in minutes, for the base test coverage set
- `refresh-timeout-seconds`: timeout used when refreshing from MongoDB

The unit does not need a snapd checkout: the charm keeps a shallow, sparse checkout of only `tests/utils/features` from `snapd-repo-url` at `snapd-ref` in `/var/lib/test-execution-lister/snapd`, updated on install, upgrade and config changes, and before every `mongodb-updated`. The `query_features.py` there must match how the data was uploaded; for example, coverage stored in its own collection needs a `query_features.py` that reads it.

The charm installs `git` and `python3-venv` with apt when missing, and creates a virtualenv in `/var/lib/test-execution-lister/venv` with `pymongo` (4.9 or newer, from PyPI), in which the refresh helper and `query_features.py` run. The dashboard packages in the features folder's `requirements.txt` are not installed.

Failures while installing or configuring leave the unit blocked or waiting with the reason; run the `reconcile` action (or change the config) to retry. Upgrading the charm always restarts the API service.

## Juju secret format

The charm expects `mongodb-secret-id` to point to a Juju secret whose content contains either:

- a JSON string under one of these keys:
  - `mongodb-credentials-json`
  - `mongodb_credentials_json`
  - `credentials-json`
  - `credentials_json`
  - `json`
- or direct top-level keys named `host`, `port`, `user`, and `password`

The preferred format is a single JSON string, for example:

```json
{
  "host": "mongodb.example.internal",
  "port": 27017,
  "user": "snapd",
  "password": "secret"
}
```

Example secret creation:

```bash
juju add-secret test-execution-lister-mongodb mongodb-credentials-json='{"host":"mongodb.example.internal","port":27017,"user":"snapd","password":"secret"}'
juju config test-execution-lister mongodb-secret-id=<secret-id>
```

## Actions

Actions are defined in [charmcraft.yaml](charmcraft.yaml):

- `calculate-tests`: accepts `backend-systems`, `changed-files-json`, and `tests`, then returns the tests that should be executed
- `add-tests`: appends tests to `added-spread-tests.txt`
- `mongodb-updated`: rebuilds `spread-execution-map.json`, `interface-users.json` and `base-executions.json`, then clears `added-spread-tests.txt`
- `reconcile`: rebuilds charm-managed files and service state
- `service-status`: returns the systemd status for the API service

Example action calls:

```bash
juju run-action test-execution-lister/0 calculate-tests \
  changed-files-json='{"modified":["snap/foo.go"],"added":["interfaces/new.go"],"deleted":[]}' \
  tests='amd64:ubuntu-24.04:tests/spread/foo,arm64:ubuntu-24.04:tests/spread/foo' \
  backend-systems='amd64:ubuntu-24.04,arm64:ubuntu-24.04'

juju run-action test-execution-lister/0 add-tests \
  tests='tests/spread/foo,tests/spread/bar'

juju run-action test-execution-lister/0 mongodb-updated
```

## Files on the unit

Default paths:

- `/var/lib/test-execution-lister/mongodb-credentials.json`
- `/var/lib/test-execution-lister/spread-execution-map.json`
- `/var/lib/test-execution-lister/added-spread-tests.txt`
- `/var/lib/test-execution-lister/base-executions.json`
- `/var/lib/test-execution-lister/service-config.json`

## Checking the logic against recent snapd commits

`tools/check_recent_commits.py` runs the same `calculate-tests` logic as the API over the most recent first-parent commits of a snapd ref, using `spread -list` at that ref as the allowed tests, and prints how many tests each commit would run and why:

```bash
tools/check_recent_commits.py --snapd ~/source/snapd --data-dir /tmp/tel \
  --credentials ~/mongo-creds.json --commits 20 --fetch --details
```

- `--data-dir` holds `spread-execution-map.json` and `base-executions.json`; they are built from mongodb with the refresh helper when missing (or with `--refresh`), which needs `--credentials` and `pymongo` available to `python3`. Spread lists are cached there per commit.
- `--systems` and `--suites` choose what is listed (default: the openstack systems in the map, `tests/main/`); `--focus` picks the system shown in the table.
- `--show-tests <commit>` prints the selected tests for one commit, and `--json` writes all results.

## Notes

- A changed file inside a test folder, `tests/<suite>/<test>/...` for the spread suites listed in `SPREAD_SUITES` in `src/service_api.py` (e.g. `tests/main/foo/task.yaml` or `tests/nested/manual/bar/script.sh`), selects that test, including all its variants, for the backend:systems whose `tests` input contains it. A changed test that is not in the input (e.g. a deleted test) selects nothing.
- Any other changed file under `tests/` (shared helpers such as `tests/lib/`, tooling, or files directly in a suite folder) returns every allowed test.
- For a modified `interfaces/builtin/<name>.go`, the tests that connected that interface in the feature data (`interface-users.json`, written on refresh next to the map) are selected in addition to the file's coverage. Many interface files have no coverage of their own, since their code runs in shared files, so with this data such a file never makes `calculate-tests` return everything: an interface no test connects selects nothing extra. Without `interface-users.json` (or without an entry for the system), the previous behaviour applies.
- Changes under `docs/` (the API documentation site) and `.github/` (CI configuration) are ignored entirely: they select no tests and never make `calculate-tests` return everything.
- Changed files that are not Go have no coverage data, so any change to one (C code such as snap-confine, packaging, `data/`, `spread.yaml`, `go.mod`, CI configuration, ...) returns every allowed test, except for files with a rule of their own: spread tests and their helpers under `tests/` (see above), documentation (`.md`, `.rst`), C unit tests (`*-test.c`, `*-test.h`) and files in unit test folders (`testdata`, folders ending in `test`, `testutil`, `testtime`).
- Only Go source files are used when resolving changed-file lookups; Go unit test files (`_test.go`) and unit test helper packages (folders ending in `test`, plus `testutil`, `testtime` and `testdata`, but not `asserts/assertstest`, which the fake store uses) are ignored because they never appear in spread coverage.
- `calculate-tests` requires structured JSON (`changed-files-json`) with `modified`, `added`, and `deleted` keys.
- `calculate-tests` requires an allowed tests list (`tests`) and uses the same selection logic as `src/calculate_tests.py`.
- Tests that failed in the coverage run are left out of `spread-execution-map.json`, so, like any allowed test missing from the map, they are always selected until a run where they pass replaces the data.
- The API service keeps the parsed `spread-execution-map.json`, `interface-users.json` and `base-executions.json` in memory and reloads a file only when it changes (its modification time or size), so requests do not re-read the large map; the refresh helper replaces the files atomically.
- Base tests are recalculated on MongoDB refresh using `query_features.py feat cover` with `--max-minutes` taken from charm config. When `base-tests-max-minutes` is above 0, `--force-match-keywords refresh` is also passed, so features mentioning refresh are always covered by the base tests even if that exceeds the time limit.
- Tests listed in `added-spread-tests.txt` are always selected, but only for backend:systems whose `tests` input contains them; an entry without a variant selects all of its variants. The result of `calculate-tests` is always a subset of the `tests` input.
- `mongodb-updated` first updates the `tests/utils/features` checkout, then runs the helper command, which in turn uses `query_features.py` to query MongoDB. The action waits for up to `refresh-timeout-seconds` plus a minute.
