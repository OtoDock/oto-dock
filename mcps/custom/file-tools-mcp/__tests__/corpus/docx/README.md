# Word corpus

Documents the write_docx tests edit as they are, beside the shared corpus's
Word files (a Collabora save and a LibreOffice save). Each file is scrubbed of
personal data: `test_docx_corpus.py` unzips every file here and checks.

| File | Made by | What it brings |
|---|---|---|
| `libreoffice-7.4-lists.docx` | LibreOffice 7.4.7 (Debian 12): a document write_docx made, re-saved with `soffice --headless --convert-to "docx:MS Word 2007 XML"` | LibreOffice's own numbering for numbered headings and lists at three levels, its own styles and section, a styled table, a header with a page number field, tab stops with a dot leader, a rule, Greek text; no paragraph ids |
| `libreoffice-7.4-review.docx` | LibreOffice 7.4.7: a contract write_docx annotated, re-saved the same way | LibreOffice's own table of contents (in a content control, entries linked to `_Toc` bookmarks), a footnote and an endnote with its 0/1 separators, two comments on the same words without `commentsExtended` (LibreOffice 7.4 keeps no reply threads), a bookmark and a link to it, tracked insertions and a deletion by role names; no paragraph ids |
| `word-markup-handbuilt.docx` | python-docx's package with Word's markup written by hand: it is not a file Word saved, and its `app.xml` names no application | paragraph and text ids, rsids, proofing and bookmark markers between runs, a template token split over three runs, a tracked insertion and deletion, a Table of Contents content control with a TOC field, a table with `gridBefore`, a vertical merge and a repeating-section row, a text box in `mc:AlternateContent` whose Fallback repeats the Choice's ids, a header with a PAGE field |

The generator is kept with the showcase, outside the shipped folder. A
document saved by Microsoft Word and one exported by Google Docs belong in
the shared corpus.
