# PDFs: write_pdf, edit_pdf and reading

The guide's routine applies: plan, build, read each result, look with
`screenshot_document`, at most three fix rounds. On this version
`check_document` has no checks for PDFs yet, so the screenshots are the
check.

## Making a PDF: write_pdf

- `content` in Markdown (the default) or HTML (`content_type` `html`),
  `page_size` A4 (the default) or Letter, `margins` such as `2cm`, and
  extra `css`.
- Pictures load only from this session's folders (by path) or `data:`
  addresses. A web address renders as missing while the call still reports
  success, so download the picture into the workspace first.
- LaTeX math renders as vector equations: `\( \)` inline, `\[ \]` or
  `$$ $$` for display. Dollar amounts such as $100 are never read as math,
  and code blocks are never scanned. An equation that fails is left as
  plain text and named in the result.
- A fillable form: write it in HTML with inputs (text, textarea, checkbox,
  radio, select) and pass `form_fields: true`; each input becomes a field
  named by its name attribute.
- For a document the person will edit later, write a Word file with
  `write_docx` and convert it with `convert_document` when they need the
  PDF. A PDF made from Markdown is for reading, not for editing.

## Changing a PDF: edit_pdf

`operations` run in order on one file and save once. The batch is all or
nothing, as in the other write tools, with `best_effort` to keep what
worked, and `fix` to resend only the corrections after a refusal.

- pages: `merge`, `split`, `rotate_page`, `delete_page`, `reorder_pages`,
  `insert_page`, `crop_page`;
- content: `add_text`, `add_image`, `add_watermark`, `add_annotation`
  (highlight, underline, strikeout, a note, a rectangle). Text is drawn in
  an embedded font, so Greek and accents show in every viewer;
- `replace_text` edits text already on the page in a matching font; an
  empty replacement deletes it;
- `redact` removes text or an area for good: the content is gone from the
  file, not hidden;
- `encrypt`, `decrypt`, `compress`, `set_metadata`, `extract_images`,
  `ocr`.

Page numbers: a page written as text (`"3"`, `"1-3"`, `"-1"`) counts from
1, as the person counts; a `page` number or a list of numbers counts from 0.
Positions are points from the page's top left.

Files written beside the PDF (a `split`'s `output_path`, extracted images,
an OCR copy) land only when the batch saves. Name them to the person by
their workspace paths, as markdown links.

## Forms

1. `read_document` with `mode` `form_fields` lists every fillable field
   with its name, kind, value and options. On a printed form with no
   fields, it lists the page's text with positions instead.
2. One `fill_form` with every value: `values` maps a field's name to its
   text, true or false for a check box, or one of its options. Every
   value is checked before anything is written; text in any script shows.
3. `flatten_form` (or `flatten: true`) draws the values into the page so
   nothing stays fillable. Do it only when the person wants the form final.
4. Read the form again to confirm, and look at the page.

A printed form without fields takes `fill_flat_form`: each entry is placed
beside a label found on the page (`label` and `text`), or at a `page`, `x`
and `y` taken from the read.

## Passwords and protection

- `password` opens a protected PDF for `read_document`,
  `screenshot_document`, `pdf_to_images` and `edit_pdf`. An edited PDF
  keeps its protection, and so do the copies an edit writes (a split, an
  OCR copy).
- Removing a protection (`decrypt`) or replacing it (`encrypt` on a
  protected file) needs the owner password; without it the operation is
  refused and says so.
- The file's owner may restrict what can be done (changing its content, its
  pages, filling its form, copying its content out). An ordinary edit still
  goes ahead, and the result warns that the file keeps its restrictions:
  tell the person. Images are not taken out of a file whose owner forbids
  copying, unless the owner password is given: `extract_images` and
  `pdf_to_images` refuse it. Extracted images carry no password; the
  result warns.
- A protected PDF cannot be merged into another: its pages would lose
  their protection. Remove the protection first, with the owner password,
  if the person wants it merged.
- Never guess a password or look for a way around one. Ask the person for
  it; without it, say the file cannot be opened or changed that way.

## Reading a PDF

- `read_document` returns the text, `pages` (`1-5`) for part of a long
  file. Pages with no text layer are reported as scanned.
- **Scanned pages**: read them with your own eyes through
  `screenshot_document`, up to ten pages a call; `dpi: 300` for
  handwriting and fine print. A call can stop short on its picture budget;
  the result names the page to continue from. Pages count from 1.
- `ocr` in `edit_pdf` makes printed text searchable. It never reads
  handwriting. Always set `language` (`ell` for Greek, `ell+eng` for Greek
  and English): the default English misreads other alphabets. A long
  document takes minutes and grows the file; `output_path` writes a
  searchable copy and leaves the original as it is.

## Pictures of pages

- `screenshot_document` is for you: the pictures show in your context, and
  no file is written. `grid: true` puts up to 36 pages on one labelled
  picture: look at a whole document first, then at single pages.
- `pdf_to_images` writes page pictures (PNG or JPEG, a `dpi` of your
  choice) to keep or embed; the first shows in the chat.
- `images_to_pdf` binds pictures into a PDF (A4, Letter or each picture's
  own size; `fit` contain, cover or stretch).
