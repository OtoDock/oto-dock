---
name: file-tools-guide
description: "How to deliver Word documents, Excel workbooks, PowerPoint decks and PDFs with the file-tools MCP that are right when the person opens them: the delivery routine (plan, build with operations, check_document, screenshots, at most three fix rounds), how to speak about the file, and each format's rules for reports, contracts under review, budgets, models, board decks and PDF forms. Read before you create or edit any document, spreadsheet, presentation or PDF, read a scanned one, or edit a photo."
---

# File tools: the guide

The card in your instructions holds the rules for every call. This guide is
the routine for delivering a file and the facts per format. Four references
beside this file go deeper into one format each; read the one you need
before a larger job in that format:

- `references/word.md`: reports, letters, contracts and templates.
- `references/excel.md`: budgets, models, data workbooks and CSV imports.
- `references/powerpoint.md`: decks, and the design rules behind them.
- `references/pdf.md`: making, editing and reading PDFs, scanned pages.

When you cannot open them, this page is enough to do the job well.

## Which tool

| The job | Tool |
|---|---|
| Read a file, find where to edit | `read_document` |
| Make or change a Word document | `write_docx` |
| Make or change an Excel workbook | `write_xlsx` |
| Make or change a PowerPoint deck | `write_pptx` |
| Make a PDF from Markdown or HTML | `write_pdf` |
| Change a PDF (pages, text, forms, redaction, passwords, OCR) | `edit_pdf` |
| Find a file's problems, made any way | `check_document` |
| See pages as pictures, for yourself | `screenshot_document` |
| Show a file in the chat | `preview_document` (automatic after a write) |
| Convert between formats | `convert_document`, `pdf_to_images`, `images_to_pdf` |
| Edit or inspect a photo | `edit_image`, `analyze_image` (recipes: the skill `photo-editing-guide`) |
| A chart picture for a document | `create_chart` |

## The delivery routine

You finish the file alone. The person sees it in the preview when you are
done, so it must be right without a reviewer.

1. **Plan.** Before the first write, set down the shape: a deck as one line
   per slide (a title that states the point, and the visual that proves it),
   a report as its outline, a workbook as its blocks (inputs, calculations,
   outputs). For an existing file, read it first.
2. **Build with operations**, in a few batches of related changes. Read
   `{"op": "help", "name": "<op>"}` before an operation you have not used in
   this session. Where no operation reaches, a `write_docx` or `write_pptx`
   batch can carry an `xml_patch`: read the part first, and read the result
   back after; that write's `Checks:` also hold the file against the Office
   file format. Your own code (python on a copy) comes last; then run
   `check_document` on the result and call `preview_document` yourself.
3. **Read every result.** Its parts come in this order: the first line
   (saved, or "Nothing saved" and why) with a count of each operation that
   applied, which should match what you sent, and of any that failed
   ("failed and changed nothing", or "failed and may be partly applied");
   `Failed:` with a fix per
   operation; `Changed nothing:`; `Warnings:`; `Notes:` (what was made and
   where); `Checks:` (the fast checks of what was saved); then, for a
   workbook, the readback.
4. **Check and look.** Before you say it is done, run `check_document` on
   the file even when every write's checks were clean: a write skips the
   completeness checks. Then look: `screenshot_document` with `grid: true`
   puts a whole deck or report on one picture (up to 36 pages), and single
   pages (up to ten per call) show the detail. The checks catch what you
   cannot see (formula errors, leftover tokens, text too small); the
   pictures catch what the checks cannot (a crowded slide, a half-empty
   one, a table that breaks badly).
5. **Fix and stop.** Fix what the findings and the pictures show, then check
   and look again. At most three rounds. Then stop and tell the person
   plainly what is left, if anything.
6. **Answer briefly**: what you made or changed, the numbers that matter,
   anything the person must decide. The preview is already open.

Findings marked "already in the file" were there before your edit. Mention
them only when they matter to the request.

`check_document` with `render: true` is slower and worth it on a finished
deck, where it measures the drawn text against its boxes
(`pptx.rendered_overflow`), and on a finished workbook: it recalculates
one made elsewhere and prints it, so a chart cut by a page break comes
back (`xlsx.print_layout`). For a document, "No check of this format
reads the rendering yet" describes the tool: your screenshots are the look.
`checks` runs only the check ids or prefixes you name (`xlsx.print_layout`).

## How a batch works

- `operations` is a list of `{"op": "<name>", ...}` objects, applied in
  order and saved once. Nothing is written until the whole batch has run.
- **All or nothing.** One failed operation means nothing is saved and the
  file is unchanged. Each failure is listed with its fix, and a later
  failure may say it follows from an earlier one. A batch with an unknown
  operation or key is refused before anything runs, with the accepted names.
- **Resend only the fix.** A refusal keeps the batch for 10 minutes and
  shows how to send `fix`, in place of `operations`, with just the
  corrections (replace, remove or add after an operation's number), up to
  three rounds. A save, a newer batch or a changed file ends it.
  `edit_pdf` keeps one too; `edit_image` does not.
- `best_effort: true` saves the operations that worked. The first line
  then counts each failed operation as "failed and changed nothing" (it
  left the file as it was) or "failed and may be partly applied" (check
  that part of the file). Use it only when a partial result is what
  the person wants, and say what failed.
- `create_new: true` starts a fresh file at the path instead of editing it.
- Invalid LaTeX in an equation fails its operation: rewrite the equation.
- All or nothing and `best_effort` hold for `edit_pdf` and `edit_image`
  too. Files an operation writes beside the original (a `split`, extracted
  images, an OCR copy) land only when the batch saves.
- A refused `xml_patch` fails its operation and leaves the part as it
  was: under `best_effort` it counts as "failed and changed nothing". Later
  operations in a batch see what an earlier patch changed.
- **Build a large file in parts**: a few slides, one report section or one
  sheet per call, each result read before the next. A refusal then costs
  one part, not the file.

## Talking to the person

Keep it simple for them: they think in slides, pages, paragraphs and cells,
not in the tool's internals.

- Say "slide 3", "the second paragraph", "the title", "the table under
  Results", "the Total row". Cell addresses such as B7 are fine in a
  spreadsheet.
- Leave out paragraph ids (#1A2B3C4D), block addresses (p12, t0/r1/c2),
  shape ids, slide and table indexes (they count from 0, people count from
  1: `slide_index` 2 is slide 3), operation names, check ids, the readback
  grid and the result's first line.
- A file that opens in the preview: name it by its file name alone, with no
  path or link, not even in backticks. A file that does not (a CSV, a picture, a second output):
  give the workspace path you saved it to as a markdown link, such as
  `[sales.csv](/users/alex/workspace/sales.csv)`; in a chat the person
  clicks it to open the file. `send_file` hands over a download.
- Do not add cautions the tool already rules out. A stored shrink scale is
  what PowerPoint shows; the preview shows a workbook's computed values.
- Answer in the person's language, even when the file is in another one.
- Screenshots, the contact sheet included, are for you and write no file.
  A deck's contact sheet from `screenshot_document` labels each slide with
  the `slide_index` `write_pptx` takes, from 0: add one when you speak of
  it. When the person wants to see the result, the preview is open; a
  picture to keep comes from `pdf_to_images` on a PDF.

## Editing a file someone else made

- Read it first with `read_document`. Match what the file already does
  (its styles, layouts, colours, number formats); the rules in this guide
  are for new work.
- Act by address. Word: `target` takes an address or id from the read, and
  `expect_text` stops the edit if the text there changed since the read.
  PowerPoint: a shape by `shape_id`, `shape_name` or `shape_index` as the
  read shows it. Excel: A1 ranges.
- `check_document` also reports a file Office would refuse or repair, and
  anything an edit dropped from the original. Fix those first.
- A file can hold things no operation models (a pivot table, a slicer,
  SmartArt). Leave them alone. If write_xlsx refuses an edit because a save
  would drop such content, ask the person before you send `allow_loss: true`.

## Word in short

- New documents are A4 portrait. Headings with `add_heading` (real heading
  styles: they drive the outline, the navigation and the table of
  contents), never bold paragraphs posing as headings.
- Lists with `add_list` (nine levels; a numbered list starts at 1 unless
  `continue`). Paragraph `style` for Quote, Caption and the template's own
  styles.
- `search_and_replace` changes every match and keeps its formatting, in the
  body, headers, footers and notes. `fill_template` fills `{{ key }}`
  tokens and reports any left; `strict: true` fails on a missing value.
- `add_table_of_contents` lists the headings with their page numbers. After
  headings change, `update_table_of_contents` (or `update_fields`, which
  also renumbers captions and refreshes cross-references) brings it up to
  date.
- Review work: `track: true` or an `author` makes a paragraph, table-row,
  replacement or note edit a tracked change (headings, tables and
  formatting are never tracked); `add_comment` and `add_footnote`
  annotate; `accept_all_changes` or `reject_all_changes` settle changes
  (all, one reviewer's, or one block's).
  Hand a reviewed document over with its changes and comments unless the
  person asks you to settle them.
- `add_page_break` is never the last operation.
- More: `references/word.md`.

## Excel in short

- Write formulas, never results you computed yourself. Every save stores the
  computed values with the formulas, and the readback opens with
  `Formulas: N, computed: N, errors: E`: errors 0 is the goal, and each
  error comes with its fix.
- XLOOKUP, XMATCH, LET, FILTER, SORT, UNIQUE and SEQUENCE are computed.
  The engine lacks a few newer Excel functions (GROUPBY, PIVOTBY,
  PERCENTOF, the REGEX functions, TRIMRANGE, and on some installs
  TEXTSPLIT, TEXTBEFORE, LAMBDA and the stacking functions). The result
  names every formula it could not compute: Excel computes those when the
  file opens, so never promise the person a number from one.
- Functions that reach the network or run a program (WEBSERVICE, IMAGE,
  RTD, CALL and others) are refused. New workbooks print on A4.
- A new workbook starts with one sheet named Sheet, and `sheet` defaults to
  the first sheet: rename it with `rename_sheet` rather than adding another.
- `import_csv` brings a CSV into a sheet; codes with a leading zero stay
  text. Inserting or deleting rows and columns moves every reference.
- To see more cells than the readback shows, use `read_document` with
  `start_cell` and `end_cell`; never write and clear a scratch cell.
- More: `references/excel.md`.

## PowerPoint in short

- New decks are 16:9. Plan one line per slide before drawing: a title that
  states the finding, and its visual (a native chart, a table, shapes or a
  picture).
- Set the look first with `set_theme` (one of ten kits, or your own colours
  and fonts), then draw in theme slots (`accent1` to `accent6`, `dk1`,
  `lt1`, `dk2`, `lt2`): the deck stays consistent and a new theme recolours
  it.
- Give a shape a `name` when you draw it and refer to it by that name;
  Morph pairs shapes by name across two slides.
- Nothing under 12 pt; titles about twice the body size; one margin and
  one gap size across the deck.
- An overflow finding names the size that fits: follow it, or set `autofit`
  to `shrink`, which stores the size PowerPoint shows.
- `add_slide` takes the deck's own layout names (the read lists them).
- `fill_template` fills `{{ key }}` tokens on every slide, in tables and in
  the notes, and reports any left; `strict: true` fails on a missing value.
- Transitions and animations come last, as a finishing pass; LibreOffice
  and Collabora show Morph as a fade and drop the more dramatic transitions.
- More: `references/powerpoint.md`.

## PDF in short

- `write_pdf` makes a PDF from Markdown or HTML, with A4 or Letter pages.
  LaTeX math renders as vector equations (`\( \)` inline, `\[ \]` or `$$ $$`
  for display); an equation that fails stays as plain text and is reported.
- `edit_pdf` merges, splits, rotates, reorders, stamps text and pictures
  (Greek included), replaces existing text in place (`replace_text`),
  removes content for good (`redact`), fills forms, adds passwords and
  compresses.
- Forms: `read_document` with `mode` `form_fields` lists the fields, their
  kinds and options; one `fill_form` fills them (every value is checked
  first); `flatten_form` makes the values final. A printed form without
  fields takes `fill_flat_form`.
- Protection: `password` opens a protected PDF, and an edited PDF keeps its
  protection; copies it writes (a split, an OCR copy) keep it too. Removing
  or changing a protection needs the owner password. An edit the owner
  restricted still goes ahead, and the file keeps its restrictions: tell the
  person. A protected PDF cannot be merged into another. Never guess a
  password: ask the person for it.
- More: `references/pdf.md`.

## Scanned pages and OCR

`read_document` reports pages with no text layer as scanned. Read them with
your own eyes: `screenshot_document` takes up to ten pages per call, and
`dpi: 300` keeps handwriting and fine print readable. A batch can stop
short on its picture budget; the result names the page to continue from.
OCR (`edit_pdf`'s `ocr`) only makes printed text searchable: always set
`language` (`ell+eng` for Greek and English), and expect a long document to
take minutes. Work on a copy when the original must stay as it is.

## Images and conversions

- `edit_image` runs a list of photo operations in order (size, crop, tone,
  colour, detail, text, borders, background removal, blurring a region);
  read `analyze_image` first. The skill `photo-editing-guide` has the
  recipes.
- `convert_document` turns Word, Excel and PowerPoint into PDF and CSV into
  a workbook; Markdown and HTML go through the same renderer as `write_pdf`.
  `pdf_to_images` writes page pictures to keep; `images_to_pdf` binds
  pictures into a PDF.
- `create_chart` draws a chart picture to embed in a document. A workbook
  or deck gets a live chart from the `add_chart` operation of `write_xlsx`
  or `write_pptx` instead.

## Mathematics

Equations travel as LaTeX. Transcribe them from photos yourself, and
compute or verify with python (sympy) in your sandbox. Write them with
`write_docx`'s `add_equation` (native and editable in Word) or an inline
`{"equation": "..."}` run, `write_pdf`'s math delimiters, or the
`add_equation` of `write_pptx` and `write_xlsx` (a sharp picture; the
workbook keeps the LaTeX in a cell comment). `read_document` gives Word
equations back as LaTeX.

## Limits

- A huge file fails with an error naming the fix: `pages` for a PDF,
  `start_cell` and `end_cell` for a workbook, `start_at` for a long Word
  document, `slides` for a deck, a lower `dpi`. Follow it.
- Saves are atomic: an interrupted write never damages the file.
