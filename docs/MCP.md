# SCD Reporting MCP server

`scripts/scd-mcp` is a [Model Context Protocol](https://modelcontextprotocol.io)
server that exposes every token-API route as an MCP tool, so Claude Code,
Claude Desktop or any MCP client can post and retrieve entries, run reports and
generate summaries. The same instructions, pre-filled with the server URL, are
in the application at **`/mcp/`** (footer link "MCP").

## How it works

```
MCP client ──stdio JSON-RPC──▶ scd-mcp (local, your machine) ──HTTPS + Bearer token──▶ /api/* on SCD Reporting
```

* Transport: stdio, newline-delimited JSON-RPC 2.0. The client launches the process.
* Protocol: initialize-handshake versions `2024-11-05`, `2025-03-26`,
  `2025-06-18`, `2025-11-25`. Unknown methods, including the `server/discover`
  probe of `2026-07-28` clients, get `-32601`, which makes those clients fall
  back to the handshake. This is verified against the official SDK client (`mcp` 2.3.0, `mode="auto"`).
* Implementation: Python standard library only (Python ≥ 3.9). The official
  SDK needs Python ≥ 3.10, so it is used only as a test client.
* Identity: your API token. The assistant can do exactly what you can, and
  every export it makes is in the audit log under your name.
* Settings: shared with the `scd-*` CLI tools. The precedence is command line > environment >
  `.env` > `~/.config/scd-reporting/config.yaml` > default (see [CLI.md](CLI.md)).
  A missing URL or token does not stop the server: each tool call then returns an explanatory error.

## Set up

```bash
git clone https://github.com/fermitools/SCD-Reporting.git ~/SCD-Reporting
mkdir -p ~/.config/scd-reporting
cat > ~/.config/scd-reporting/config.yaml <<'EOF'
url: https://scd-reporting.fnal.gov
token_file: ~/.config/scd-reporting/token
EOF
read -rs -p "Paste API token: " T && printf '%s\n' "$T" > ~/.config/scd-reporting/token && unset T
chmod 600 ~/.config/scd-reporting/token
python3 ~/SCD-Reporting/scripts/scd-whoami          # confirms URL + token
```

Register it:

| Client | How |
|---|---|
| Claude Code, all projects | `claude mcp add --scope user scd-reporting -- python3 ~/SCD-Reporting/scripts/scd-mcp` |
| Claude Code, Windows | `claude mcp add --scope user scd-reporting -- py "$HOME\SCD-Reporting\scripts\scd-mcp"` |
| Claude Code, this repo | `.mcp.json` is committed; start `claude` in the checkout and approve *scd-reporting* |
| Claude Desktop | `claude_desktop_config.json` → `{"mcpServers": {"scd-reporting": {"command": "python3", "args": ["/abs/path/scripts/scd-mcp"]}}}` |

Check it with `claude mcp list` (it should report *Connected*), `/mcp` inside a session, or
`scripts/scd-mcp --list-tools`.

## Tools

| Tool | API route | Kind | Notes |
|---|---|---|---|
| `scd_whoami` | `GET /api/whoami/` | read | role, group, report scope, formats |
| `scd_list_taxonomy` | `GET /api/taxonomy/[<kind>/]` | read | slugs/ids for every filter |
| `scd_list_my_entries` | `GET /api/entries/` | read | structured, paged (default 25) |
| `scd_get_entry` | `GET /api/entries/<id>/` | read | json, md, txt, csv, xlsx, pdf |
| `scd_export_my_entries` | `GET /api/entries/?format=` | read | all matches; audited |
| `scd_create_entry` | `POST /api/entries/` | **write** | `period` shortcut sets dates + `period_kind` |
| `scd_run_report` | `POST /api/reports/` | read | reporting role; audited |
| `scd_count_report` | `POST /api/reports/?count=1` | read | reporting role |
| `scd_report_summary` | `POST /api/reports/summary/` | **paid** | reporting role; model call |

Tool annotations: `readOnlyHint` is true for everything except `scd_create_entry`, `destructiveHint` is false for all tools,
`openWorldHint` is true. JSON results come back both as text and as
`structuredContent` (protocol ≥ 2025-06-18). API failures (400/401/403/404)
are returned as `isError: true` tool results carrying the server's message and
per-field errors, so the model can correct itself. An unknown tool name is a JSON-RPC `-32602`.

Arguments mirror the API parameters (see [CLI.md](CLI.md#api-reference)), plus:

* `period`: `today`, `this-week`, `last-week`, `this-month`, `last-month`,
  `this-quarter`, `last-quarter`, `this-year`, `last-year`
* `output_path`, `overwrite`: save the result to a file. Required for xlsx and pdf, and for text over 400 000 characters.
  The server never replaces a file unless `overwrite` is true.

Unknown arguments are rejected (not ignored), so a misspelt filter cannot silently widen a report.

## Restricting what it can do

| Restriction | Flag | Env / config |
|---|---|---|
| No entry creation | `--read-only` | `SCD_MCP_READ_ONLY=1` / `mcp_read_only: true` |
| No paid summaries | `--no-summary` | `SCD_MCP_NO_SUMMARY=1` / `mcp_no_summary: true` |

Disabled tools are not listed at all. Revoking or regenerating the token on
your Profile cuts off access immediately.

## Tests

```bash
venv/bin/pip install -r requirements-dev.txt      # includes the mcp SDK (Python >= 3.10) for conformance
venv/bin/python -m pytest tests/test_scd_mcp.py
SCD_CLIENT_PYTHON=/usr/bin/python3.9 venv/bin/python -m pytest tests/test_scd_mcp.py -k "EndToEnd or Sdk"
```

`TestEndToEnd` drives the real `scd-mcp` process over stdio against a live test
server: it posts an entry and reads it back, runs reports, and checks file output, role errors and validation errors.
`TestSdkConformance` connects with the official SDK client.
