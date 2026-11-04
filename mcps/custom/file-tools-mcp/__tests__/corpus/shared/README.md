# Shared corpus

Files saved by other applications, for the tests that must find nothing on
what Office-compatible editors write untouched. Each is kept as saved, except
that a personal name in `docProps/core.xml` is cleared (the LibreOffice deck
inherited one from python-pptx's template).

| File | Saved by | Made from |
|---|---|---|
| `collabora-26.04-walk.docx` | Collabora Office 26.04.5.1 | a python-docx document edited and saved in the Collabora editor |
| `collabora-25.04-check.xlsx` | Collabora Office 25.04.9.4 | an openpyxl workbook edited and saved in the Collabora editor |
| `libreoffice-7.4-report.docx` | LibreOffice 7.4.7 | a python-docx report (headings, a list, a table, a page break, Greek text) re-saved by LibreOffice |
| `libreoffice-7.4-budget.xlsx` | LibreOffice 7.4.7 | an openpyxl budget (formulas, a bold header, frozen panes, a bar chart, Greek text) re-saved by LibreOffice |
| `libreoffice-7.4-deck.pptx` | LibreOffice 7.4.7 | a python-pptx 16:9 deck (title, bullets, a chart, a table, notes, Greek text) re-saved by LibreOffice |

## Files saved by Microsoft Office and Google, from other projects' test data

Taken unchanged from other projects' published test files at a pinned commit,
except that author and company names in `docProps` are cleared. The
application is the one the file names itself (`docProps/app.xml`, or the
summary stream of an old binary file); Google's exports carry no app.xml, so
those are named from their own XML. The legacy `.doc` and `.xls` files carry
no author name and are byte for byte the source.

| File | Saved by | Source (repository, commit, path) | Licence | Changed |
|---|---|---|---|---|
| `word-2016-blk-inner-content.docx` | Microsoft Office Word 16.0000 | python-openxml/python-docx `e45454602b53` `tests/test_files/blk-inner-content.docx` | MIT | unchanged |
| `word-2010-poi-56076.docx` | Microsoft Office Word 14.0000 | apache/poi `17de566ac64d` `test-data/document/bug56076.docx` | Apache-2.0 | `Company` cleared |
| `word-2013-tika-table-in-run.docx` | Microsoft Office Word 15.0000 | apache/tika `20676c0c3d0a` `tika-parsers/tika-parsers-standard/tika-parsers-standard-modules/tika-parser-microsoft-module/src/test/resources/test-documents/testWORD_tableInRunInsideParagraph.docx` | Apache-2.0 | `dc:creator` cleared, `cp:lastModifiedBy` cleared |
| `excel-2016-exceljs-567.xlsx` | Microsoft Excel 16.0300 | exceljs/exceljs `5bed18b45e82` `spec/integration/data/test-pr-567.xlsx` | MIT | `dc:creator` cleared, `cp:lastModifiedBy` cleared |
| `excel-2016-calamine-391.xlsx` | Microsoft Excel 16.0300 | tafia/calamine `f2b565420508` `tests/issue_391.xlsx` | MIT | unchanged |
| `excel-2010-chartsheets.xlsx` | Microsoft Excel 14.0300 | foss.heptapod.net/openpyxl/openpyxl (branch/3.1) `c7b9026dab21` `openpyxl/reader/tests/data/contains_chartsheets.xlsx` | MIT | unchanged |
| `excel-online-poi-formula-eval.xlsx` | Microsoft Excel Online 16.0300 | apache/poi `17de566ac64d` `test-data/spreadsheet/formula-eval.xlsx` | Apache-2.0 | unchanged |
| `powerpoint-mac-2010-minimal.pptx` | Microsoft Macintosh PowerPoint 14.0000 | scanny/python-pptx `278b47b1dedd` `tests/test_files/minimal.pptx` | MIT | `Company` cleared, `dc:creator` cleared, `cp:lastModifiedBy` cleared |
| `powerpoint-2007-poi-sample.pptx` | Microsoft Office PowerPoint 12.0000 | apache/poi `17de566ac64d` `test-data/slideshow/sample.pptx` | Apache-2.0 | unchanged |
| `powerpoint-2016-poi-smartart.pptx` | Microsoft Office PowerPoint 16.0000 | apache/poi `17de566ac64d` `test-data/slideshow/SmartArt.pptx` | Apache-2.0 | `dc:creator` cleared, `cp:lastModifiedBy` cleared |
| `google-docs-poi-61787.docx` | Google Docs export (no app.xml; the export's structure and styles) | apache/poi `17de566ac64d` `test-data/document/61787.docx` | Apache-2.0 | unchanged |
| `google-docs-eng-spa.docx` | Google Docs export (no app.xml; the export's structure and styles) | Unstructured-IO/unstructured `910d9f41e70a` `example-docs/language-docs/eng_spa_mult.docx` | Apache-2.0 | unchanged |
| `google-sheets-exceljs.xlsx` | Google Sheets export (named in its own XML) | exceljs/exceljs `5bed18b45e82` `spec/integration/data/hidden-test/google-sheets.xlsx` | MIT | unchanged |
| `google-sheets-conditional-formatting.xlsx` | Google Sheets export (named in its own XML) | PHPOffice/PhpSpreadsheet `ed591ed22e43` `samples/templates/BasicConditionalFormatting.xlsx` | MIT | unchanged |
| `google-slides-table.pptx` | Google Slides export (named in its own XML) | Unstructured-IO/unstructured `910d9f41e70a` `example-docs/fake-power-point-table.pptx` | Apache-2.0 | unchanged |
| `excel-2016-greek-named-range.xlsx` | Microsoft Excel 16.0300 | PHPOffice/PhpSpreadsheet `ed591ed22e43` `tests/data/Worksheet/namedRangeTest.xlsx` | MIT | `dc:creator` cleared, `cp:lastModifiedBy` cleared |
| `word-2003-poi-56880.doc` | Microsoft Office Word 11.8132 | apache/poi `17de566ac64d` `test-data/document/56880.doc` | Apache-2.0 | unchanged |
| `word-2013-tika-control-characters.doc` | Microsoft Office Word 15.0000 | apache/tika `20676c0c3d0a` `tika-parsers/tika-parsers-standard/tika-parsers-standard-modules/tika-parser-microsoft-module/src/test/resources/test-documents/testWORD_specialControlCharacter1415.doc` | Apache-2.0 | unchanged |
| `excel-97-poi-54500.xls` | Microsoft Excel 8.3411 | apache/poi `17de566ac64d` `test-data/spreadsheet/54500.xls` | Apache-2.0 | unchanged |
| `excel-2016-calamine-merged-range.xls` | Microsoft Excel 16.0000 | tafia/calamine `f2b565420508` `tests/merged_range.xls` | MIT | unchanged |

The Apache-2.0 files come with these notices: "Apache POI. Copyright
2003-2026 The Apache Software Foundation. This product includes software
developed at The Apache Software Foundation (https://www.apache.org/)." and
"Apache Tika. Copyright 2007-2026 The Apache Software Foundation. This
product includes software developed at The Apache Software Foundation
(http://www.apache.org/)."

Not in the corpus yet, because no honestly sourced file was found: Greek
body text saved by Microsoft Office or Google (only a Greek defined name),
a PowerPoint `.ppt` with no author name, Word 2007 and Excel 2007 files.

No file carries personal data: authors are the libraries' names or empty,
and no part holds an e-mail address or a home folder (a test reads every
file, old binary files byte by byte, to hold this).
