"""Tool catalogue shown on the /mcp/ instructions page.

Kept in the app (rather than imported from scripts/scd_mcp.py, which is client
code) and checked against scd_mcp.TOOLS by tests/test_scd_mcp.py so the page
cannot drift from the server.
"""

# (name, access, needs reporting role, summary)
MCP_TOOLS = [
    ('scd_whoami',            'read',  False, 'Account behind the token: role, group, report scope, formats.'),
    ('scd_list_taxonomy',     'read',  False, 'Projects, categories, groups, entry types, lab priorities and tags with their slugs.'),
    ('scd_list_my_entries',   'read',  False, 'Your own entries as structured JSON, with paging.'),
    ('scd_get_entry',         'read',  False, 'One entry by id (JSON, Markdown or any export format).'),
    ('scd_export_my_entries', 'read',  False, 'All matching entries of your own in txt, md, csv, json, xlsx or pdf.'),
    ('scd_create_entry',      'write', False, 'Post a new entry under your name.'),
    ('scd_run_report',        'read',  True,  'Reports-page report over your report scope, in any export format.'),
    ('scd_count_report',      'read',  True,  'Number of entries a report would contain.'),
    ('scd_report_summary',    'paid',  True,  'AI summary of a report selection (a paid model call).'),
]

REPO_URL = 'https://github.com/fermitools/SCD-Reporting'
