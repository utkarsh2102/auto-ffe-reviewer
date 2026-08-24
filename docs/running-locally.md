# Running it locally

## Install

```sh
git clone <this repository>
cd auto-ffe-reviewer

python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

On Ubuntu, one system package supplies the release calendar offline:

```sh
sudo apt install distro-info-data
```

`ubuntu-dev-tools` is optional. The evidence layer fetches the same upstream
endpoints directly, because `seeded-in-ubuntu` and `reverse-depends` set no
timeout and were observed hanging indefinitely. The tools are only used for an
optional cross-check.

**No Launchpad credentials are needed.** Everything is a public read.

## The commands

```sh
ffe discover                  # what is awaiting the Release Team right now
ffe calendar                  # where the development release is in its cycle
ffe evidence --bug 2167691    # gather everything for one bug; no model involved
ffe review --bug 2167691      # add a recommendation
ffe run                       # one full cycle, as the scheduled job runs it
ffe publish                   # regenerate the dashboard
ffe learn --dry-run           # disagreements worth learning from
ffe lessons                   # precedents learned so far
ffe fingerprint --bug N       # why a bug was or was not re-reviewed
ffe policy-hash               # the policy version and hash
```

### Start here

`ffe evidence --bug <N>` is the one worth knowing. It gathers everything and
prints it, without spending a model call or writing any state:

```
LP #2167484  FFe: rename "sudo" binary package to "sudo.ws"

  Release   stonking (26.10) INTERIM, phase UI_FREEZE, 25 days to release
  Packages  sudo

  sudo
    seeded on   ubuntu-core-installer
                (seed index looks incomplete (index covers only 3 flavours);
                 the listed images are real but there may be more)
    rdepends    59 runtime, 1 build

  Testing   ppa=ABSENT build=ABSENT tests=ABSENT output=ABSENT
  Risk      75/100 SEVERE
  Gates
    [floor_needs_information] no_testing_evidence: nothing in the bug
      demonstrates the change works: no PPA, build log or test results
```

Caveats print next to the value they qualify, as above: a number you cannot
fully trust should say so where you read it.

## Without a model

Everything except the final recommendation works with no model at all:

```sh
ffe review --no-llm
```

You still get the evidence, the risk score with every component's reasoning,
and the gates. On a request with no testing evidence, that is usually the whole
answer.

## Useful flags

```sh
ffe --offline evidence --bug N       # cached evidence only; opens no sockets
ffe --state-dir /tmp/state run       # keep state somewhere scratch
ffe review --bug N --force           # re-review even if nothing changed
ffe evidence --bug N --json          # the full bundle, for scripting
```

## Development

```sh
pytest -q                 # 570+ tests, no network, no API key
ruff check src tests
ruff format src tests
mypy src/ffe
```

The suite blocks sockets outright. A test that needs the network must be marked
`@pytest.mark.network`, and CI excludes those. Fixtures under
`tests/fixtures/raw/` are real captured upstream responses, so the production
parsers run against bytes Ubuntu actually publishes.

To refresh a fixture, re-fetch the URL recorded in the relevant source module
and commit the result — the shape is occasionally worth re-checking, since
upstream formats change without notice.

## Where things are stored

By default in `.ffe-state/`:

```
state.json                        per-bug tracking and budgets
reviews/<bug>/<fingerprint>.json  immutable; one per distinct evidence state
archive/<bug>.json                decided bugs, with the team's verdict
lessons/<id>.json                 learned precedents
site/                             the generated dashboard
```

Serve the dashboard locally with:

```sh
python3 -m http.server -d .ffe-state/site 8000
```
