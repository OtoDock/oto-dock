# Third-party notices

The file tools are OtoDock's own code under the Functional Source License
(FSL-1.1-ALv2). The files named below carry material from other projects,
copied unchanged or adapted. Each entry names the project, its licence, where
it comes from, the files that carry its material and what was changed; for an
Apache-2.0 project, its NOTICE file is reproduced in full. Each licence's text
is in the `licenses/` folder beside this file, under the name the entry
gives, and every derived source file keeps its upstream header with a line
saying it was changed by OtoDock. A project the file tools only took an idea
from has no entry.

### Open XML SDK (MIT)

- Upstream: https://github.com/dotnet/Open-XML-SDK, commit
  431ab05cf160248cc3885a4a766026d4f8243792 (`data/schemas`, `data/namespaces.json`)
- Files: `ooxml/order_table.json`, generated from that data by
  `ooxml/generate_order.py` (its `source` and `license` keys name both)
- Changes: only the order of each element's children is kept (names, ranks,
  and the type of a child whose name has several types)
- Copyright: Copyright (c) .NET Foundation and Contributors
- Licence: `licenses/Open-XML-SDK-MIT.txt`, the project's `LICENSE`

### Caladea, crosextrafonts 2013 (Apache-2.0)

- Upstream: https://dev-www.libreoffice.org/src/368f114c078f94214a308a74c7e991bc-crosextrafonts-20130214.tar.gz
  (sha256 c48d1c2fd613c9c06c959c34da7b8388059e2408d2bb19845dc3ed35f76e4d09),
  the Caladea build LibreOffice and Collabora Office ship (version 1.002)
- Files: none in this folder; the image installs `Caladea-Regular.ttf`,
  `Caladea-Bold.ttf`, `Caladea-Italic.ttf` and `Caladea-BoldItalic.ttf`
  unchanged in `/usr/local/share/fonts/caladea/`, with the Apache License 2.0
  text beside them as `LICENSE`
- Copyright: Copyright (c) 2012 Huerta Tipografia. Caladea is a trademark of
  Huerta Tipografia
- Changes: none
- NOTICE: the archive carries no NOTICE or licence file; each font states its
  licence in its own name table ("Licensed under the Apache License, Version 2.0")
- Licence: `licenses/Caladea-Apache-2.0.txt`, the Apache License 2.0 text,
  the same as the `LICENSE` beside the fonts

### ppt-master (MIT)

- Upstream: https://github.com/hugohe3/ppt-master, commit 15baa7c5
  (`skills/ppt-master/scripts`)
- Files: `formats/pptx/pptx_transitions.py` (the `REGISTRY` table),
  `formats/pptx/pptx_animation_presets.json`
- Changes:
  - `pptx_transitions.py`: the transition table of `pptx_transitions.py`
    (each effect's element, namespace and attributes, and the direction,
    orientation and Morph options) kept as data; the writer re-implemented
    on lxml
  - `pptx_animation_presets.json`: 24 of the PowerPoint-recorded effect rows
    of `pptx_animation_presets.json` copied, the presentationml prefix
    written as `p`
- Copyright: Copyright (c) 2025-2026 Hugo He
- Licence: `licenses/ppt-master-MIT.txt`, the project's `LICENSE`

### theme-factory (Apache-2.0)

- Upstream: https://github.com/anthropics/skills, commit
  3b3fad96af16a10759d930941b4520ba0c40edae, `skills/theme-factory`
- Files: `formats/pptx/pptx_theme.py` (the `KITS` table)
- Changes: the ten palettes' names and colours taken (23 of the 40 colours
  unchanged), each four-colour palette derived into the theme's twelve colour
  slots (light text lifted to a near-white, accent 1 darkened until white
  text reads 4.5:1 on it, the other accents to 3:1 on the background), the
  fonts swapped for Office fonts with metric twins
- Copyright: Copyright 2026 Anthropic, PBC
- NOTICE: none (the upstream has no NOTICE file)
- Licence: `licenses/theme-factory-Apache-2.0.txt`, the package's own
  `LICENSE.txt`

### ECMA-376 transitional XML schemas (LicenseRef-Ecma-text-copyright)

- Upstream: Ecma International, ECMA-376 Office Open XML File Formats, 5th
  edition, Part 4 (Transitional Migration Features), Annex A
  (`OfficeOpenXML-XMLSchema-Transitional.zip`)
- Files: `ooxml/xsd/ecma/*.xsd`, read by `ooxml/schema_checks.py`
- Changes: none; the files are byte-identical to Ecma's, and
  `ooxml/xsd/ecma/NOTICE` lists the source and each file's sha256
- Copyright: © Ecma International
- Licence: `licenses/ECMA-376-LicenseRef-Ecma-text-copyright.txt`, Ecma's
  default copyright notice, also in `ooxml/xsd/ecma/NOTICE`

## Entry format

```
### <Project> (<SPDX licence id>)

- Upstream: <repository URL>, <version or commit>
- Files: <files in this folder that carry its material>
- Changes: <one line>
- Copyright: <the upstream copyright line>
- NOTICE: <for Apache-2.0, the upstream NOTICE text, verbatim>
- Licence: `licenses/<Project>-<SPDX licence id>.txt`, the upstream text unchanged
```
