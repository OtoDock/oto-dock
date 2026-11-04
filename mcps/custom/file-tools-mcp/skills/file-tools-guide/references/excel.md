# Excel workbooks with write_xlsx

The guide's routine applies: plan, build in batches, read each result, run
`check_document`, look with `screenshot_document`, at most three fix rounds.
This page holds the Excel detail. `{"op": "help", "name": "<op>"}` shows any
operation's keys and examples; read it before `add_chart` and
`conditional_format`.

## Planning a workbook

- Lay it out in blocks: the inputs (assumptions, one labelled cell each,
  with its unit), the calculations, then the outputs (totals, ratios, a
  chart). Every number that is not an input is a formula that reads the
  inputs, so changing an input updates the whole model.
- Headers name the unit ("Revenue (EUR)", "Growth %"). Freeze the header
  row (`freeze_panes`), and give every numeric range a number format.
- In a new model, colour tells the reader what may be changed: inputs in
  blue font, formulas in black, links to other sheets in green. An existing
  workbook keeps the look it already has.
- One sheet per purpose (Inputs, Model, Summary) once a model grows past
  one screen.

## Writing cells

- A1 notation: columns are letters, rows count from 1; ranges `B2:D10` or
  `Data!A1:C7`. `sheet` defaults to the first sheet.
- A new workbook starts with one sheet named Sheet. Rename it
  (`{"op": "rename_sheet", "old_name": "Sheet", "new_name": "Budget"}`)
  rather than adding a second sheet that the other operations would miss.
- `write_cells` takes `cells` (one object per cell, with an optional
  `type` and `format`) or `data` (a grid of rows) with `start_cell`.
  Formulas are text starting with `=`.
- Dates: strict ISO text (`2026-03-27`, `2026-03-27T14:30`, `14:30`)
  becomes a real date; `27/03/2026` stays text and the result warns.
- `number_format` and a cell's `format` take a preset (`date`, `percent`,
  `number`, `integer`, `currency`, `text` and others) or an Excel code.
- `set_style` changes only the properties you give; the rest of each cell's
  style is kept.
- The result shows a readback grid of what was written, with each formula's
  value in its cell's format. To see more cells, use `read_document` with
  `start_cell` and `end_cell` (or `show_formulas: true` for the formulas);
  never write and clear a scratch cell.

Example batch for the top of a budget:

```json
[
  {"op": "write_cells", "start_cell": "A1", "data": [["Item", "Q1", "Q2", "Total"], ["Revenue", 120000, 135000, "=SUM(B2:C2)"], ["Costs", 80000, 82000, "=SUM(B3:C3)"], ["Margin", "=B2-B3", "=C2-C3", "=D2-D3"]]},
  {"op": "set_style", "range": "B2:D4", "number_format": "currency"},
  {"op": "set_style", "range": "B2:C3", "font_color": "1F4E9E"},
  {"op": "freeze_panes", "cell": "A2"}
]
```

## Values and recalculation

- Every save recalculates the workbook and stores the computed values with
  the formulas, so the preview, a phone and `read_document` show numbers.
- The readback opens with `Formulas: N, computed: N, errors: E`. Errors 0
  is the goal. Each error is also an `xlsx.formula_error` finding with its
  fix (`#DIV/0!`: guard the divisor with IF; `#REF!`: the formula points at
  deleted cells; `#NAME?`: a misspelled function or name).
- "kept from before this edit: K" means K formulas kept the value the file
  had: the engine could not compute them and nothing they read changed.
- XLOOKUP, XMATCH, LET and the dynamic arrays (FILTER, SORT, SORTBY,
  UNIQUE, SEQUENCE) are computed.
- **Functions the engine lacks** are computed by Excel when the file opens,
  and their cells (and every cell reading them) are saved without a value.
  The result names them: "N formulas use functions the calculation engine
  lacks". Among them: GROUPBY and PIVOTBY; PERCENTOF; the REGEX functions;
  TRIMRANGE. Depending on the install, also TEXTBEFORE, TEXTAFTER,
  TEXTSPLIT, the stacking and slicing family (VSTACK, HSTACK, TAKE, DROP and
  the like), and LAMBDA with MAP, REDUCE, SCAN, BYROW, BYCOL and MAKEARRAY.
  When the person will read the file in the preview or on a phone, prefer
  a function the result reports as computed. Never promise a number such a
  formula has not produced, and say that Excel fills it in.
- CELL and INFO are never saved with a value: they would describe the
  server.
- A workbook that reads outside data (another workbook, a data connection,
  RTD) is saved without a recalculation, and the result says so. Its
  formulas keep the values the file had.
- `check_document` with `render: true` recalculates a copy and prints it.
  Run it once on a finished workbook, made any way: it finds a chart or
  picture cut by a page break, and columns that run onto another page on a
  sheet with no page setup of its own (`xlsx.print_layout`). It also gives
  values to a workbook made some other way, or after an
  `xlsx.not_computed` finding; after write_xlsx they are already stored.

## Refused formulas

A formula that calls WEBSERVICE, IMAGE, STOCKHISTORY, TRANSLATE,
DETECTLANGUAGE, COPILOT, RTD, CALL, REGISTER, EXEC, PY or SQL.REQUEST, or
holds a DDE link, is refused wherever this batch would store it, and the
whole batch is refused with the cell named. Formulas the file already had
are kept. Tell the person to write the value itself, or use a function that
reads only the workbook.

## Content a save would drop

Editing a workbook that holds what this tool cannot keep (slicers, form
controls, shapes and text boxes, threaded comments, sparklines, data
connections, macros in a file named `.xlsx`, and more) is refused with
"Nothing saved" and a "Would be lost:" list. Ask the person. Then send the
same batch with `allow_loss: true`, or suggest they make the change in
Excel. Never set `allow_loss` without their agreement. Macros survive in
`.xlsm` and `.xltm`.

## Structure edits

`insert_rows`, `delete_rows`, `insert_columns`, `delete_columns`,
`move_rows` and `move_columns` move every formula, name, rule, validation,
chart series, table and picture that reads the cells. Inserting inside a
summed range grows the SUM. References to deleted cells become `#REF!`.
The note counts what was updated. `rename_sheet` updates every formula,
name and chart that names the sheet.

## CSV data

`import_csv` reads a CSV from the workspace into a sheet: `delimiter`
(detected when left out), `encoding` (`cp1253` for Greek Windows files),
`decimal` (`,` for numbers written 1.234,56), `header` and `types`.
Numbers and strict ISO dates are typed; codes with a leading zero and digit
strings over 15 digits stay text, and the note counts them. The header row
is bold, frozen and filtered. Afterwards format the columns with
`set_style`, and `create_table` if the person wants a table.

## Charts, tables and formatting

- `add_chart` takes `data_range` (categories in the first column, series
  names in the first row) or `categories` with `series` (each with `name`
  and `values` as a range or a list), placed at an `anchor` cell. Series
  can read any rows or columns in place; no helper block is needed. A list
  of numbers is written to a block under the sheet's data. Give it a
  `title` and axis titles.
- `create_table` makes a real Excel table with a style; `auto_filter`,
  `add_data_validation` (drop-down lists) and `conditional_format` (cell
  rules, formulas, colour scales, data bars, icon sets) add the rest.
- `set_column_width` or `auto_column_width` widen columns.
  `xlsx.hash_overflow` warns when a formatted number is wider than its
  column, which Excel shows as `#####`.
- `set_print_setup` sets paper, orientation and scaling. New workbooks
  print on A4; `fit_width: 1` keeps a wide sheet on one page across.
  Put a chart under its table, not beside it, unless the sheet fits one
  page across.

## The Excel checks

After each write, `Checks:` lists what was found; `check_document` adds the
final ones:

- `xlsx.formula_error`: a formula that evaluates to an error;
- `xlsx.broken_ref`: references to deleted cells, missing sheets, broken
  names or chart series;
- `xlsx.spill`: a formula that spills into several cells but is saved as
  a plain formula;
- `xlsx.long_number`: an id stored as a number (Excel keeps 15 digits);
- `xlsx.hash_overflow`: a number too wide for its column;
- `xlsx.not_computed` (in `check_document`): formulas saved without a
  value, which a reader that does not calculate shows empty;
- `xlsx.roundtrip_loss` (in `check_document`): content a later write_xlsx
  edit would drop or simplify;
- `xlsx.office_refuses`: a table name or number format Excel would repair
  away when it opens the file;
- `xlsx.package`: something Excel would refuse or repair;
- `xlsx.schema` (in `check_document`): a part that breaks the Office file
  format rules, which Excel may repair. A warning;
- `xlsx.parts_lost`: content an edit dropped from a file someone else made.
  Fix this one first;
- `xlsx.print_layout` (in `check_document` with `render: true`): how the
  workbook prints. A chart or picture cut by a page break, or a sheet with
  no page setup of its own whose columns run onto another page.

Findings the file already had are listed apart.

## Telling the person

Speak of sheets, rows and cells as they see them ("the Total row", "cell
F5"). Give the numbers that matter ("total revenue 3.03 M, margin 37.4%"),
not the readback or the check names. Say when a value is left for Excel to
compute, and why.
