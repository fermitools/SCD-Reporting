# SCD Reporting command-line tools and token API

Six small tools for getting data out of SCD Reporting from a terminal, plus the
token-authenticated `/api/` endpoints they use. All of them authenticate with
your personal API token (Profile → API Token); no browser session, SSO cookie
or CSRF handling is involved.

| Tool | Endpoint | Who | What |
|---|---|---|---|
| `scd-report` | `GET/POST /api/reports/` | Reporter roles¹ | Reports-page filters → txt / md / csv / json / xlsx / pdf, or `--count` |
| `scd-summary` | `POST /api/reports/summary/` | Reporter roles¹ | AI summary of the same selection → md / txt / json |
| `scd-entries` | `GET /api/entries/` | Anyone | Your own entries: table, JSON, or any export format |
| `scd-entry` | `GET /api/entries/<id>/` | Anyone² | One entry |
| `scd-taxonomy` | `GET /api/taxonomy/[<kind>/]` | Anyone | Slugs/ids for projects, categories, groups, entry types, lab priorities, tags |
| `scd-whoami` | `GET /api/whoami/` | Anyone | Your role, group, report scope; `--settings` shows resolved config |

¹ Auditor, Group Leader, Division Head, Functional Lead or Admin — the roles that
can open the Reports page. Results are scoped exactly as on that page.
² Visibility as on the web entry page.

The existing `POST /api/entries/` (create) and `scd-post-entry` / `scd-post-entry-json` are unchanged.

## Install

The tools are single Python files in `scripts/` and need only the standard
library (Python ≥ 3.9). PyYAML is used for the config file if installed;
otherwise a simple `key: value` reader is used.

```bash
# Linux / macOS: put the tools on PATH and the man pages on MANPATH
git clone git@github.com:fermitools/SCD-Reporting.git
export PATH="$PWD/SCD-Reporting/scripts:$PATH"
export MANPATH="$PWD/SCD-Reporting/man:$(manpath 2>/dev/null)"

# or install copies
install -m 755 SCD-Reporting/scripts/scd_client.py SCD-Reporting/scripts/scd-{report,summary,entries,entry,taxonomy,whoami} ~/.local/bin/
install -m 644 SCD-Reporting/man/scd-*.1 ~/.local/share/man/man1/
install -m 644 SCD-Reporting/man/scd_client.3 ~/.local/share/man/man3/
```

The `scd-*` wrappers import `scd_client.py` from their own directory (symlinks
are followed), so keep them together.

**Windows 11:** run through the interpreter — either the wrapper or the module's
sub-command form:

```powershell
py scripts\scd-report -p dune --period last-month -f md -o dune.md
py scripts\scd_client.py report -p dune --period last-month -f md -o dune.md
```

## Configure

```bash
mkdir -p ~/.config/scd-reporting
cp config/scd-client.yaml.example ~/.config/scd-reporting/config.yaml
# paste your token from Profile > API Token:
printf '%s\n' 'PASTE-TOKEN-HERE' > ~/.config/scd-reporting/token
chmod 600 ~/.config/scd-reporting/token
scd-whoami            # verifies URL + token
scd-whoami --settings # shows every value and where it came from
```

Precedence per key: **command line > environment > `.env` > config file > default**.

| Config key | Env var | CLI | Default |
|---|---|---|---|
| `url` | `SCD_URL` | `-u/--url` | — (required) |
| `token` | `SCD_TOKEN` | `-t/--token` | — (required unless `token_file`) |
| `token_file` | `SCD_TOKEN_FILE` | `--token-file` | — |
| `timeout` | `SCD_TIMEOUT` | `--timeout` | 300 s |
| `ca_bundle` | `SCD_CA_BUNDLE` | `--ca-bundle` | system store |
| `insecure` | `SCD_INSECURE` | `-k/--insecure` | false |
| `format` | `SCD_FORMAT` | `scd-report -f` | md |
| (file path) | `SCD_CONFIG` | `-c/--config` | `~/.config/scd-reporting/config.yaml` |

`.env` files: `~/.config/scd-reporting/.env`, then `./.env` (cwd wins). Only
`SCD_*` keys are read, so a server `.env` in the repo cannot leak other settings
into the client.

## Use

```bash
# Markdown report of last month's DUNE entries
scd-report -p dune --period last-month > dune-oct.md

# Entries overlapping an explicit range, two projects, as Excel
scd-report -p dune -p nova --from 2026-10-01 --to 2026-10-31 --overlap -f xlsx -o oct.xlsx

# Count only (no audit record)
scd-report --category scientific --period this-quarter --count

# Search syntax is the Reports page's: OR by default, AND/OR/XOR, quoted phrases
scd-report -s 'timing AND "trigger board"' -f json | jq '.[].title'

# AI summary, Markdown to a file
scd-summary -p mu2e-daq --period last-week -o daq-week.md

# Your own entries
scd-entries --period last-month
scd-entries -p nova -f md -o my-nova.md
scd-entry 1234 | less

# Find slugs to filter on
scd-taxonomy projects
```

Output goes to stdout unless `-o FILE` or `-O` (server-suggested file name) is
given; `xlsx`/`pdf` are refused on a terminal. Exit status: 0 ok, 1 server
rejected the request (status and per-field errors on stderr), 2 usage/config
error, 3 server unreachable.

Named periods (`--period`): `today`, `this-week`, `last-week` (Monday-based),
`this-month`, `last-month`, `this-quarter`, `last-quarter` (calendar),
`this-year`, `last-year`. `--from`/`--to` override either end.

Full option lists: `man scd-report`, `man scd-summary`, `man scd-entries`,
`man scd-entry`, `man scd-taxonomy`, `man scd-whoami`; Python API: `man 3 scd_client`.

## Python library

```python
import sys; sys.path.insert(0, "SCD-Reporting/scripts")
from scd_client import Client, load_settings, period_range

c = Client.from_settings(load_settings())          # same config resolution as the CLI
start, end = period_range("last-month")
rows = c.report("json", projects=["dune"], period_after=start.isoformat(),
                period_before=end.isoformat(), date_match="overlap")
md = c.report("md", projects=["dune"]).text()
n = c.report_count(search="timing")
summary = c.summary("json", projects=["dune"])["text"]
for e in c.iter_entries(q="timing"):
    print(e["id"], e["title"])
```

Errors raise `ScdApiError` (`.status`, `.message`, `.errors`),
`ScdNetworkError` or `ScdError`.

## API reference

All endpoints require `Authorization: Bearer <token>`. Session cookies are
**not** accepted (the endpoints are CSRF-exempt, so accepting cookies would make
them forgeable cross-site). A token whose user is deactivated is rejected.
Errors are JSON: `{"error": "..."}` or `{"errors": {"field": "message"}}`.

Parameters may be sent as a query string, a form body, or a JSON object body;
lists may be repeated keys (`projects=dune&projects=nova`), comma-separated
(`projects=dune,nova`) or JSON arrays.

### `GET|POST /api/reports/`

| Parameter | Meaning |
|---|---|
| `format` | `json` (default), `txt`, `md`, `csv`, `xlsx`, `pdf` — the same exporters as the web download |
| `count` | `1` → `{"count": N}` instead of a file |
| `search` | title/description search (AND/OR/XOR, quoted phrases) |
| `author_email` (`author`) | substring match |
| `projects` (`project`), `categories` (`category`), `lab_priorities` (`lab_priority`) | slugs or ids; match any |
| `group`, `employee_group`, `entry_type` | slug or id |
| `period_after`, `period_before` | `YYYY-MM-DD` |
| `date_match` | `contained` (default) or `overlap` |
| `is_private`, `exclude_private` | `true` |
| `show_archived` | `true` → archived entries only |
| `selected_ids` (`ids`) | restrict to these entry ids |

Unknown slugs and invalid values return 400 rather than an empty report.
Responses: 200 file, 400, 401, 403 (role). Every non-count call writes an
`export` audit record with `source: "api"`.

```bash
curl -sS -H "Authorization: Bearer $SCD_TOKEN" \
  "$SCD_URL/api/reports/?format=md&projects=dune&period_after=2026-10-01&period_before=2026-10-31"
```

### `POST /api/reports/summary/`

Same filters, plus `format` = `json` (default: `{count, text, truncated,
stop_reason, input_tokens, output_tokens, max_tokens}`), `md` or `txt`, and
`template` (name or id of one of your saved prompt templates). 400 for an empty
selection, a missing model key or an over-size input; 502 if the model call
fails. Blocks until the summary is complete.

### `GET /api/entries/`

Your own entries. `q` (title/description/tag substring), `projects`,
`categories`, `period_after`/`period_before` (overlap), `archived`
(`0` default, `1`, `all`), `limit` (1–1000, default 100), `offset`, `format`.
`format=json` returns `{count, limit, offset, results:[entry…]}`; any other
format returns all matches as a file (paging ignored; audited).

Entry object: `id, title, description, author{email,display_name},
projects[{id,slug,name}], categories[…], lab_priorities[…], entry_type, group,
tags[], period_kind, period_start, period_end, is_private, is_critical,
is_highlight, highlight_stars, is_division_head_only, is_archived, created_at,
updated_at, url`.

`POST /api/entries/` (create) is unchanged.

### `GET /api/entries/<id>/`

One entry; `format` as above (default `json`). 404 if it does not exist or you
may not see it.

### `GET /api/taxonomy/` and `GET /api/taxonomy/<kind>/`

`kind` ∈ `projects, categories, entry-types, groups, lab-priorities, tags`.
`all=1` includes inactive values. Rows: `{id, slug, name, short_code, is_active,
sort_order}`; tags: `{id, name, use_count}`.

### `GET /api/whoami/`

`{id, username, email, display_name, role, role_label, group, can_report,
report_group_scope, report_project_scope, export_formats, token{created_at,
last_used_at}}`. A `null` scope means unrestricted.

## Tests

```bash
venv/bin/python -m pytest tests/test_api_read.py tests/test_scd_client.py
# run the CLI subprocesses under another interpreter (e.g. the 3.9 floor):
SCD_CLIENT_PYTHON=/usr/bin/python3.9 venv/bin/python -m pytest tests/test_scd_client.py -k EndToEnd
```

`test_scd_client.py::TestEndToEnd` starts a live test server and runs the real
`scd-*` executables against it as subprocesses.
