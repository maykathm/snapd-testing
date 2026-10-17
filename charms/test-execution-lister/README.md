# test-execution-lister

`test-execution-lister` is a machine charm that exposes a small local API for deciding which spread tests should run from a set of changed files, tracking manually added tests, and refreshing the test map from MongoDB through `query_features.py` from the snapd repository.

## What it does

The charm manages three pieces of state on the unit:

- a MongoDB credentials JSON file written from a Juju secret
- `spread-execution-map.json`, used to look up tests by backend and changed Go file
- `added-spread-tests.txt`, used to carry tests that should always be included

The service listens on `listen-host:listen-port` and can handle multiple requests in parallel.

## Configuration

Available config options are defined in [config.yaml](config.yaml):

- `listen-host`: host for the local API service, default `127.0.0.1`
- `listen-port`: port for the local API service, default `8181`
- `mongodb-secret-id`: Juju secret ID containing the MongoDB credentials JSON
- `query-features-path`: absolute path to `query_features.py` on the unit
- `query-features-requirements-path`: optional override for the `requirements.txt` used by `query_features.py`; by default the charm uses the sibling file next to `query_features.py`
- `execution-data-dir`: directory containing `spread-execution-map.json` and `base-executions.json`
- `added-spread-tests-path`: path to the added tests text file
- `refresh-helper-command`: optional override for the helper command that rebuilds `spread-execution-map.json` and `base-executions.json`
- `base-tests-max-minutes`: maximum execution time, in minutes, for the base test coverage set
- `refresh-timeout-seconds`: timeout used when refreshing from MongoDB

The default `query-features-path` assumes the snapd repository is available on the machine at `/home/ubuntu/source/snapd/tests/utils/features/query_features.py`. Update that value if your path is different.

The charm installs the dependencies listed in the corresponding `requirements.txt` before starting the API service or running the refresh helper.

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

Actions are defined in [actions.yaml](actions.yaml):

- `calculate-tests`: accepts `backend-systems`, `changed-files-json`, and `tests`, then returns the tests that should be executed
- `add-tests`: appends tests to `added-spread-tests.txt`
- `mongodb-updated`: rebuilds `spread-execution-map.json` and `base-executions.json`, then clears `added-spread-tests.txt`
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
- Only Go source files are used when resolving changed-file lookups; Go unit test files (`_test.go`) are ignored because they never appear in spread coverage.
- `calculate-tests` requires structured JSON (`changed-files-json`) with `modified`, `added`, and `deleted` keys.
- `calculate-tests` requires an allowed tests list (`tests`) and uses the same selection logic as `src/calculate_tests.py`.
- Base tests are recalculated on MongoDB refresh using `query_features.py feat cover` with `--max-minutes` taken from charm config.
- Tests listed in `added-spread-tests.txt` are always selected, but only for backend:systems whose `tests` input contains them; an entry without a variant selects all of its variants. The result of `calculate-tests` is always a subset of the `tests` input.
- `mongodb-updated` runs the helper command, which in turn uses `query_features.py` to query MongoDB.
