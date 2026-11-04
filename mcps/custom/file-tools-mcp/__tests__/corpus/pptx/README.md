# PowerPoint corpus

Decks the write_pptx tests edit as they are, beside the decks the tests build
with python-pptx. Each file is scrubbed of personal data:
`test_pptx_corpus.py` unzips every file here and checks.

| File | Made by | What it brings |
|---|---|---|
| `libreoffice-7.4-numbered.pptx` | LibreOffice 7.4.7 (Debian 12): a five-slide deck written by write_pptx, opened in LibreOffice Impress, saved as `.odp`, then saved as `.pptx` | LibreOffice's own masters and layouts: five masters of twelve layouts each, a title layout typed `tx`, placeholders without an `idx`, slides that point to a master's "Blank Slide" layout, slide numbers as LibreOffice writes them (every slide except the title slide), Greek text, a picture, speaker notes |
| `macro-package.pptm` | python-pptx 1.0.2 through write_pptx's own save | A macro presentation as PowerPoint packages one: the `vbaProject` relationship and the `bin` content type. Its project bytes are a stand-in, since the tools keep them and never read them |

The LibreOffice deck was made with the 1.9.0 file-tools image (Debian 12,
LibreOffice 7.4.7):
`soffice --headless --convert-to odp`, then
`soffice --headless --convert-to pptx:"Impress MS PowerPoint 2007 XML"`.

A file added here is picked up by every test that walks the folder. A deck
saved by Microsoft PowerPoint, one exported by Google Slides and a `.pptm`
with a real project belong in the shared corpus.
