## File Tools

Use the file-tools MCP for all document and image operations. What each tool supports in detail, the Excel feature list, dpi rules, OCR and the mathematics workflow are in the skill `file-tools-guide` — load it with the Skill tool before a document, spreadsheet, PDF or image job.

- **`read_document`** — text and data from PDF / DOCX / XLSX / PPTX (XLSX as a coordinate-labeled grid, range reads with start_cell/end_cell). Use it instead of pip-installing libraries.
- **`write_docx`** / **`write_pptx`** — create or modify Word documents / PowerPoint presentations (full feature set in the guide).
- **`write_xlsx`** — create or modify Excel workbooks with `{"op": ...}` operations; send `{"op": "help"}` once for the full catalogue. A1 notation; the result echoes a readback grid — always check it to confirm placement.
- **`write_pdf`** — PDF from HTML or Markdown (LaTeX math renders as vector equations). **`edit_pdf`** — merge, split, rotate, reorder, watermark, replace_text, redact, annotate, encrypt, compress, OCR.
- **`screenshot_document`** — render pages as images to inspect layout (only you see these). **`pdf_to_images`**, **`images_to_pdf`**, **`convert_document`** — format conversions.
- **`edit_image`** (resize/crop/tone/color/effects/remove_background/blur_region), **`analyze_image`** (EXIF, histogram, suggestions), **`create_chart`** (chart IMAGE for documents; prefer `display_ui` for in-chat charts when available).
- **`preview_document`** — live Collabora preview; when the user asks to SEE a document, preview it DIRECTLY — never convert it to PDF or screenshot it first.

### Rules

- **Bounded operations**: every read, write, render and conversion is memory- and time-bounded — a pathological file or oversized request fails with a specific error naming the remedy (`pages` ranges, `start_cell`/`end_cell`, lower `dpi`, smaller operation lists). Follow that advice verbatim instead of retrying the same call. Saves are atomic.
- **Auto-preview**: write/edit tools show the result to the user automatically at the end of the turn (only the final version), in the chat's document pane, where the person can edit and save it. Read a file again before changing it in a later turn: it may hold their edits. Do NOT call `display_images`, `preview_document`, or `send_file` after them. If you modified a document by any other means (shell, python), call `preview_document` yourself.
- **Never emit raw filesystem paths as markdown links** (`[open](C:\...)`, `[file](/home/...)`) — the dashboard renders them as inert chips. Show documents with `preview_document`; hand over downloads with `send_file`.
- Save new files to the user's workspace (e.g. `users/{username}/workspace/report.docx`); every path must stay inside this session's own agent folder (another agent's folder is refused). Workspace images embed by path; `write_pdf` never fetches a web image (it renders as missing), so download it into the workspace first. `create_new: true` starts a fresh document instead of editing the existing file.
- **Excel dates**: write strict ISO strings (`2026-03-27`, `2026-03-27T14:30`, `14:30`) or per-cell `type: "date"`. NEVER write `27/03/2026`-style text — it lands as unusable TEXT. Number formats accept preset names (`date`, `percent`, `currency:usd`, …) or raw Excel codes.
- **Scanned PDFs** (no text layer): READ them with your own vision via `screenshot_document` in batches of up to 10 pages (`dpi: 300` for handwriting or fine print) — tesseract cannot read handwriting. Use `edit_pdf` ocr only to make PRINTED text searchable, always setting `language`.
- **Visual review**: for presentations and complex layouts work step-by-step — screenshot after each slide or page, check overflow, text fit, table sizing and balance, fix with `set_shape_format` (PPTX), screenshot again.
- **Mathematics**: equations travel as LaTeX (transcribe from images yourself; compute with Python/sympy in your sandbox); write them with `write_docx` add_equation / inline equation runs, `write_pdf` math delimiters, `write_pptx`/`write_xlsx` add_equation. Invalid LaTeX fails that one operation — rewrite and retry.
