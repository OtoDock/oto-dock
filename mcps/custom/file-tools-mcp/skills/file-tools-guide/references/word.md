# Word documents with write_docx

The guide's routine applies: plan, build in batches, read each result, run
`check_document`, look with `screenshot_document`, at most three fix rounds.
This page holds the Word detail. `{"op": "help", "name": "<op>"}` shows any
operation's keys and examples.

## Planning a document

- Write the outline first: the headings and what each section must show.
  A report usually needs a title, a short summary up front, the sections,
  and the tables or figures that carry the numbers.
- New documents are A4 portrait with 1 inch margins. Use Letter only when
  the person asks or the file already is Letter
  (`{"op": "set_page_setup", "page_size": "letter"}`).
- Keep one body font and size, and let the heading styles carry the
  hierarchy. Spacing goes in points (`space_after`: 6 to 12 is typical),
  `line_spacing` as a multiple (1.15, `1.5x`).

## Reading and addressing

`read_document` prints every block with its address and paragraph id:

- `p12` is the thirteenth body paragraph (addresses count from 0);
- `t2/r3/c1` is the third table, its fourth row, second column, and
  `t2/r3/c1/p0` the first paragraph in that cell;
- `header0/p0` is the first paragraph of the first section's default
  header (`header0.first` and `header0.even` name its other kinds);
- `#1A2B3C4D` is a paragraph's id, which follows it wherever it moves.

A long document reads in windows: `start_at` and `end_at` take an address,
and `outline_only: true` gives the overview (pages, counts, the outline with
addresses, headers and footers).

Most operations take `target` (an address or id) and `position` (`after`
by default, or `before`; `start` or `end` of a cell, header or content
control). `expect_text` is the text the target starts with, as the read
showed it: if someone changed the document since the read, the operation
fails instead of editing the wrong place.

- Positional addresses name the document as the call began, so every
  operation of one batch can use addresses from the same read.
- An `index` key counts the document as it is at that moment.
- New blocks are listed under `Notes:` with their addresses.

Example batch on a document read a moment ago:

```json
[
  {"op": "set_paragraph_text", "target": "p4", "expect_text": "Revenue grew", "text": "Revenue grew 12 percent on the year."},
  {"op": "add_paragraph", "target": "p4", "text": "Costs held flat.", "style": "Normal"}
]
```

These addresses and ids are for you. With the person, speak of "the
paragraph under Results" or "the second table".

## Headings, styles and lists

- `add_heading` with `level` 1 to 9 (0 is the document title). Real heading
  styles drive the navigation pane, the outline and the table of contents.
  Never fake a heading with a bold paragraph.
- `number_headings` numbers the heading styles (1, 1.1, 1.1.1) so every
  heading of those levels is numbered, including ones added later.
- `style` on `add_paragraph`, `insert_paragraph` and
  `set_paragraph_format` applies a paragraph style by name: Quote, Caption,
  or a style the template defines.
- `add_list` makes a real list: `ordered` for numbers, nine `level`s, and
  each numbered list starts at 1 unless `continue` carries on the previous
  one; `start` and `number_format` set the first number and its form.
- `tab_stops` and `add_horizontal_rule` handle aligned columns of text and
  dividing lines; never line things up with spaces.

## Tables

- `add_table` with `headers` and `rows`. `style_table` gives it a header
  row, alternating row colours, a `row_fill` per row and borders.
- `set_table_column_widths` sets each column in inches; keep the total
  within the text width (A4 with 1 inch margins: about 6.3 in).
- Numbers read best right-aligned. There is no column key yet: send one
  `set_paragraph_format` with `alignment` right per cell, by `target`
  (`t0/r1/c2/p0`). One batch can hold all of them.
- `merge_table_cells`, `add_table_row` and `delete_table_row` (by
  `target` row or by index).
- `docx.wide_blocks` warns when a table or picture is wider than the page's
  text area.

## Page, headers, footers and page numbers

- `set_page_setup` changes the last section (orientation, page size,
  margins in inches). `add_section_break` starts a new section on a new
  page, for example a landscape page for a wide table.
- `add_header` and `add_footer` replace the first paragraph of a header or
  footer. `kind` is `default`, `first` (the section's first page, such as a
  cover with its own header) or `even` (even pages; odd ones take default).
- `add_page_number` puts a live number in the footer; `format` takes
  `{page}` or `{page} of {total}`.

## Links, captions and the table of contents

- `add_hyperlink` links a web address (`url`) or a place in the document
  (`bookmark`); `add_bookmark` names that place: a paragraph, or a few of
  its words with `on_text`.
- `add_caption` numbers a figure or a table ("Figure 2. Revenue by
  month."). Target the table or picture: a `Table` caption goes above it,
  any other `label` below (`position` overrides). Without a target it goes
  at the end. Give it a `bookmark` to refer to it.
- `add_cross_reference` points to a bookmark (a caption's, a heading's or
  your own) and writes its words ("Figure 2", `show` `text`) or its page
  (`show` `page`) in the target paragraph, right after `after_text`.
- `add_table_of_contents` lists the headings as the batch leaves them,
  wherever it comes in the batch, with page numbers (`page_numbers: false`
  for none). When a later batch changes the headings, run
  `update_table_of_contents`.
- `update_fields` renumbers the captions and refreshes every cross-reference
  and table of contents: run it once after moving or adding figures,
  tables or headings.

## Review: tracked changes, comments and notes

- `set_track_changes` makes Word record later edits made in Word.
- These operations take `track` and `author`, and are tracked by default in
  a document that records changes: `search_and_replace`,
  `set_paragraph_text`, `add_paragraph`, `insert_paragraph`,
  `delete_paragraph`, `add_table_row`, `delete_table_row`, `add_footnote`,
  `add_endnote` and `delete_note`. On any document, `track: true` or an
  `author` makes one of them a tracked change ("Client Legal" signs it).
- Other edits (headings, tables, lists, pictures, formatting) are never
  tracked: in a document that records changes the result warns, and
  `track` on them is refused as an unknown key.
- `accept_all_changes` and `reject_all_changes` settle changes: all of
  them, one reviewer's (`author`), or the ones inside one block (`target`
  with `expect_text`).
- `add_comment` comments a paragraph, or a few words of it with `on_text`;
  `reply_to_comment`, `resolve_comment` and `delete_comment` work on a
  thread by its address (`comment1`).
- `add_footnote` and `add_endnote` put the mark right after `after_text`;
  `delete_note` removes a note and its mark (`footnote1`).
- The read shows insertions as `{+...+}`, deletions as `{-...-}`, and every
  note and comment with its address.
- **Delivering a review**: hand the document over with its tracked changes
  and comments, so the person sees each one, unless they asked you to
  settle them. Say how many changes and comments you made.

## When no operation reaches: xml_patch

`xml_patch` edits one part's XML by XPath, with the actions `replace`,
`remove`, `insert_before`, `insert_after` and `insert_child`. It is the last
resort, after every operation:

- Read the block first. With `target` the XPath starts at that block
  (`./w:pPr`) and the patch cannot leave it; `part` reaches the styles,
  numbering, settings, theme, notes, comments, headers and footers.
- Write the fragment in the Word schema's element order; `match` `all`
  changes every element the XPath selects, `one` (the default) insists on
  exactly one.
- It refuses, with nothing changed: fields and their code, the package's
  plumbing, anything that would run, fetch or embed something when the file
  opens, Strict XML, an element out of the schema's order, a relationship
  id the part lacks, a part with an element of more than 256 attributes,
  and an XPath that steps up or joins paths.
- It also refuses to change an element that holds a field, such as a body
  with a table of contents. To add a paragraph there, select a paragraph
  with no field and use `insert_after`.
- Read the block again afterwards, and run `check_document`.

## Find, replace and templates

- `search_and_replace` changes every match and keeps each match's
  formatting, even when the text is split across runs. It reaches the body
  at any depth, every header and footer, and notes. Options:
  `match_case`, `whole_word`, `regex` (groups as `\1`), `scope`, `limit`,
  and `target` to stay inside one block.
- `fill_template` fills `{{ key }}` tokens everywhere (nested values as
  `a.b`), even when a token is split across runs. The result lists tokens
  left and keys unused; `strict: true` fails on a token with no value.
  `check_document` reports leftover tokens (`docx.leftover_tokens`) before
  you hand the document over. For a document from a `.dotx` template, copy
  it to a `.docx` path, then fill.
- `set_properties` sets the title and author shown under File, Info.

## Pictures and equations

- `add_image` takes `image_path` and `width_inches`; keep it within the
  text width.
- `add_equation` writes native Word math from LaTeX; a run
  `{"equation": "<latex>"}` puts math inside a sentence.

## The Word checks

After each write, `Checks:` lists what was found in the saved file;
`check_document` runs them all, the final ones included:

- `docx.leftover_tokens`: a `{{ token }}`, a TODO or similar left in the
  text;
- `docx.empty_heading`: a heading with no text;
- `docx.heading_jump`: a level skipped (a level 3 heading under a level 1);
- `docx.wide_blocks`: a table or picture wider than the text area;
- `docx.package`: something Word would refuse or repair when it opens
  the file;
- `docx.schema` (in `check_document`, and after a write that applies
  `xml_patch`): a part that breaks the Office file format rules, which Word
  may repair. A warning: files from Google Docs and other tools often have
  a few;
- `docx.parts_lost`: content an edit dropped from a file someone else
  made. Fix this one first;
- `docx.toc_without_entries`, `docx.toc_out_of_date`,
  `docx.toc_without_page_numbers`: a table of contents that is empty, lists
  other headings than the document has, or lacks page numbers
  (`update_table_of_contents` fixes all three);
- `docx.broken_internal_link`: a link to a bookmark that is not there;
- `docx.tracked_changes_left` and `docx.open_comments`: information, not
  faults. A reviewed document is delivered with them; settle them only when
  the person asks.

Each finding says where it is and how to fix it.
