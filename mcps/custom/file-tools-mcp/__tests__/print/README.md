# Print fixtures

Workbooks the print check (`xlsx.print_layout`) is tested on. Both were made
by `write_xlsx` from the same request, one by each engine (a renovation
budget with monthly costs by category, totals, a chart and an XLOOKUP), and
are kept as saved.

| File | Made by | How it prints |
|---|---|---|
| `budget-cut-chart.xlsx` | Claude Code | 3 A4 pages: the table's columns run onto a second page, and the chart, anchored at J3 beside the table with no page fit, is cut across pages 2 and 3 |
| `budget-one-page.xlsx` | Codex | 1 page: fitted to one page wide and tall, the chart under the table |
