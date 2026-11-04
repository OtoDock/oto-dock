# PowerPoint decks with write_pptx

The guide's routine applies: plan, build in batches, read each result, run
`check_document`, look with `screenshot_document`, at most three fix rounds.
This page holds the deck craft and the PowerPoint detail.
`{"op": "help", "name": "<op>"}` shows any operation's keys and examples.

## Plan the deck before drawing

Write the plan in your reply or a scratch note before the first write: one
line per slide, a title that states the point and the visual that proves
it.

```
1  Q3 revenue beat plan by 8%            : big number, revenue vs plan bars
2  Growth came from two new regions       : column chart by region
3  Costs rose slower than revenue         : line chart, both series
4  Hiring is the main risk for Q4         : three short blocks with icons
5  We ask for approval of the Q4 budget   : the ask and the figure
```

- A title states the finding ("Costs rose slower than revenue"), not the
  topic ("Costs"). Someone who reads only the titles gets the story.
- One idea per slide. When a slide needs more than about 40 words of body
  text, split it or move detail to the speaker notes.
- The visual follows what the data is: a series over time is a line, a
  comparison is bars or columns, a few parts of a whole is a doughnut,
  facts by rows and columns are a table, steps or relations are shapes and
  connectors. Use native charts and tables, never pictures of them.
- The last slide carries the takeaway or the ask, not a bare thank-you.

## Look and layout

- **16:9.** New decks are 13.333 by 7.5 inches. Lengths are inches unless
  given a unit (`2.5cm`, `40pt`).
- **The theme first.** `set_theme` sets the deck's colours and fonts in one
  operation: a `kit` (`ocean_depths`, `modern_minimalist`, `tech_innovation`
  and seven more), or your own `colors` and `heading_font`/`body_font`, with
  a `name` the Design tab shows. A company's brand colours go into the
  theme, not onto each shape.
- **Colours as theme slots**: `accent1` to `accent6`, `dk1` and `dk2`
  (dark), `lt1` and `lt2` (light), with an optional tint or shade: grey
  text is `dk1` lightened, text on a dark fill is `lt1`, fills are accents.
  The deck stays consistent, and a new theme recolours everything at once.
  Spread colour 60-30-10: most of the slide in the base colour, a third as
  much in a second colour, and a small share in the accent, kept for the
  number or word that matters most.
- **Sizes.** Body text 18 to 24 pt, titles about twice the body, nothing
  under 12 pt (the `pptx.small_text` check). Use at most two font
  families, usually the theme's.
- **Spacing.** Margins of 0.4 to 0.5 inches from the slide edges and one
  gap size between blocks across the whole deck. Align blocks on shared
  left and top edges. Never shrink a margin to make text fit: cut words or
  split the slide.
- **Contrast.** Text must stand out from its fill (4.5:1, or 3:1 for large
  text: the `pptx.contrast` check). A shape with text and no `fill_color`
  takes accent 1 with text that reads on it.
- **Depth with restraint.** `shadow`, `glow`, `gradient`, `transparency` and
  `soft_edges` on `add_shape` and `set_shape_format`: one of them per
  object, and a shadow on the few objects that should float above the
  slide, not on every card.

## Reading and addressing

`read_document` on a deck lists every slide with its layout, and with
`detail` `shapes` (the default) every shape with its index, id, name,
placeholder, position, size and text; `outline` gives titles and layouts
only, `text` adds each run's font. `slides` picks slides (`2-5`, `-1`).

- Slides are `slide_index`, from 0 (-1 is the last). Shapes are `shape_id`
  (stays when shapes move), `shape_name` or `shape_index`.
- `screenshot_document` shows only the slides that are not hidden, and
  counts its pages from 1: `slide_index` 2 is page 3 when no slide before
  it is hidden.
- These numbers are for you; to the person it is "slide 3".

## Slides and text

- `add_slide` takes the deck's own layout names, as the read lists them,
  or one of the kinds `title`, `title_and_content`, `section_header`,
  `two_content`, `comparison`, `title_only`, `blank`; `title` sets its
  title. A company template's layouts keep their look this way.
- `set_text` fills a placeholder (`title`, `body`, `subtitle`, `content` or
  a number from the read). `runs` mixes formatting in one paragraph; a line
  feed in `text` starts a new paragraph. `alignment` (`left`, `center`,
  `right`, `justify`) aligns its text; without it the layout's stays.
- `add_bullet_points` appends real bullets (`items` with a `level` 0 to
  8) to a placeholder or a shape.
- `add_textbox` grows to fit its text unless you give `autofit`. With
  `autofit` `shrink` the text shrinks to the box, and the size PowerPoint
  shows is stored in the file: nothing more to tell the person. `resize`
  grows the box instead.
- Text keys on these operations: `line_spacing`, `space_before`,
  `space_after`, `bullet`, `insets`, `vertical_anchor`, `autofit`, and
  `lang` (`el-GR` for Greek, so spelling and hyphenation work).
- `format_text` formats every run that holds its `find` text on one slide,
  or in one shape when `shape_id`, `shape_name` or `shape_index` names it.
  Without `find` it formats every run.
- `set_speaker_notes` holds what the presenter says: full sentences, the
  explanation the slide leaves out.
- `fill_template` fills `{{ key }}` tokens on every slide, in tables and in
  the notes, and reports any left; `strict: true` fails on a missing value.
  For a deck from a template, copy the `.potx` to a `.pptx` path, then fill.

## Shapes, tables, charts and pictures

- `add_shape` draws rectangles, rounded rectangles, ovals, arrows,
  chevrons, flowchart shapes and callouts, with text, fill and line.
  `set_shape_format` restyles, moves and resizes; `remove_shape` deletes.
  `add_connector` draws a straight line or arrow; `add_group_shape` groups
  shapes by name.
- `add_table` with `headers` and `rows`; `format_table_cell` styles a cell
  or a range (`end_row`, `end_col`) and merges with `merge_to`. Keep table
  text at 12 pt or more: split a long table over two slides.
- `add_chart`: column, bar, line, pie, doughnut, scatter, area and radar
  kinds, `series` with a `color` each (a theme slot), `colors` per slice
  for pie and doughnut, a `title`; set `has_legend` false for a single
  series. `update_chart_data` changes the numbers later.
- `add_image` keeps the picture's proportions when only `width` or
  `height` is given; `crop` trims it, `shape_type` gives it an outline (an
  oval photo), and `alt_text` says what a screen reader reads
  (`decorative: true` for an ornament).
- `name` on `add_shape` (or `set_shape_format`) names a shape as you draw
  it; later operations, connectors, animations and Morph find it by that
  name.
- `arrange_shape` brings a shape to the front or sends it back;
  `align_shapes` lines shapes up (to each other or the slide);
  `distribute_shapes` spaces three or more evenly.
- `add_connector` with `start_shape` and `end_shape` draws a line between
  two shapes (`arrow_end: true` for an arrow). It is glued to rectangles,
  rounded rectangles, text boxes, pictures and placeholders outside a
  group, and follows them when they move; the result notes a shape it
  could not glue.
- `format_chart` styles a chart already on the slide: the value axis
  (`min` 0 so bars are not exaggerated, its number format, gridlines off),
  data labels (`data_labels: true` shows the values, `{"show": "percent"}`
  the shares of a pie, `false` hides them), the legend, gap width and the
  chart's fonts.
- `set_background` sets a colour or gradient; `set_slide_number` turns on
  live slide numbers once, and slides added later follow.
- `duplicate_slide` copies a slide (its own notes and charts) to
  `to_index`; `move_slide` and `delete_slide` reorder.

## Transitions and animations: a finishing pass

- `set_transition` sets one slide's transition, so a deck takes one
  operation per slide. Use one quiet kind for the whole deck (`fade`,
  `push` or `wipe`, about 400 to 700 ms). `advance_after` makes a slide
  advance by itself, for a kiosk loop.
- `morph` moves shapes that have the same name on this slide and the one
  before: name them alike, then set `morph` on the second slide.
- LibreOffice and Collabora show Morph as a fade and drop the more dramatic
  transitions (`drape`, `curtains` and the like); the result notes it. For a
  deck people will open in the preview, keep to the kinds they show.
- `add_animation` adds an entrance, emphasis or exit effect to a named
  shape (`on_click`, `with_previous` or `after_previous`);
  `remove_animations` clears a shape's or a slide's. Animate a few shapes
  that tell the story in order, never every shape.

## When no operation reaches: xml_patch

`xml_patch` edits one part's XML (a slide, its notes, a layout, a master or
the theme) by XPath, with the actions `replace`, `remove`, `insert_before`,
`insert_after` and `insert_child`. It is the last resort, for what no
operation covers: a preset geometry's detail, a layout's colour mapping.

- Reach a shape by the name or id the read shows:
  `//p:sp[p:nvSpPr/p:cNvPr/@name='Card']`.
- A refused patch (fields, anything that would run or fetch something when
  the deck opens, a new relationship, an element out of the schema's order,
  a required child removed, a part with an element of more than 256
  attributes) fails its operation, and the part stays as it was. Operations
  after it in the batch see the patched part.
- A slide whose shapes hold a field (a slide number, a date) refuses
  `insert_child` into its shape tree. To add a shape, select another shape
  with no field and use `insert_after`: it lands on the same slide.
- Then read the slide again with `read_document` (a patched table's cells
  read back) and run `check_document`: that is how you confirm a patch. A
  chart's own XML is beyond `xml_patch`: use `format_chart` or
  `update_chart_data`.

## The deck checks

After each write, `Checks:` lists what was measured on the saved deck:

- `pptx.overflow`: text that does not fit its box; the finding names the
  size that fits. Widen or heighten the box to that size, cut words, or set
  `autofit` to `shrink`;
- `pptx.wrap_trap`: a line meant to stay on one line that LibreOffice and
  Collabora wrap;
- `pptx.off_slide`: a shape cut off by, or wholly outside, the slide edge;
- `pptx.overlap`: text covered by another shape, or running into another
  text;
- `pptx.contrast`: text too faint on its fill;
- `pptx.small_text`: text under 12 pt;
- `pptx.package`: something PowerPoint would refuse or repair;
- `pptx.schema` (in `check_document`, and after a write that applies
  `xml_patch`): a part that breaks the Office file format rules, which
  PowerPoint may repair. A warning;
- `pptx.parts_lost`: content an edit dropped from a deck someone else
  made. Fix this one first.

`check_document` adds `pptx.leftover` (template or unfinished text, empty
placeholders) and `pptx.slide_number` (numbers that will refresh when the
deck opens). With `render: true` it also runs `pptx.rendered_overflow`:
the slides are drawn, and any text that runs past its box or the slide is
named. Run that once on a finished deck.

Then look: `screenshot_document` with `grid: true` shows the whole deck on
one picture, each slide labelled with its `slide_index` (from 0); then
single slides up close with `pages`, which counts the shown slides from 1
(the slide labelled 2 is `pages` `3` when no slide is hidden), ten per call
at most. The checks
measure; the pictures show balance, crowding and alignment.
