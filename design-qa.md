# Graphite terminal design QA

- Source visual truth: `.runtime/graphite-reference.png`, the selected first Graphite concept.
- Implementation capture: `.runtime/graphite-screen.png`, actual ANSI renderer interpreted by pyte.
- Viewport: 100 columns by 40 rows; source and rendered capture are 1465 by 1074 pixels.
- State: eight synthetic conversations, fourteen routes, Mobile selected, receiver ready.
- Scope: native terminal dashboard. The terminal owns font rasterization and background; no web frontend or image assets are installed.
- Private captures remain outside Git. This report is a reviewed summary, not a published raw capture.

## Comparison history

Initial capture: blocked. The compact list occupied only the upper portion of the screen, the
header lacked the reference hierarchy, and the selected row lacked a full-width highlight.
These are P1 composition and selection issues. Required fixes: right-aligned Live, bold title
and names, aligned columns, emerald leading bar, full-width charcoal highlight and responsive
vertical spacing. Preserve a compact layout on short terminals.

Final visual comparison: `.runtime/graphite-comparison.png` places the source and implementation
side by side at equal pixel dimensions. `.runtime/graphite-detail-comparison.png` compares the
column headings, status dots and selected row. Both were opened after the layout fixes.
The revised view has a right-aligned Live indicator, aligned columns, bold conversation names,
a full-width selected background, an emerald bar and a persistent lower context. Adaptive spacing
removes the large empty center. No actionable P0/P1/P2 visual findings remain.

## Required surfaces

- Typography: monospace Menlo capture, bold title and names, regular metadata. Terminal fonts and
  cell metrics remain user-controlled; the mock's larger title and antialiasing are not portable
  terminal primitives. No image text is embedded in the product.
- Spacing: three aligned columns, thin rules, generous rows at 100 by 40; compact rows and route
  context at 36 by 12, 24 by 8 and 100 by 8. Those compact text renders retain selection and exit keys.
- Colors: charcoal selection, emerald leading marker, green/red status, cyan Live, gray rules.
  Default terminal background and configurable ANSI palette are respected. Colorless mode retains
  shapes and a selection marker.
- Image assets: none; the selected concept is terminal text and selection decoration. The
  background texture in the generated concept is intentionally not installed into a terminal.
- Copy: connection counts and activity are real renderer fields. Fixture labels are illustrative,
  not hardcoded product titles. Route selection and snapshot age are explicit operational context
  added to the mock; delivery wording does not imply completed agent work.

## Implementation checklist

- [x] Match the chosen conversation overview and selection hierarchy.
- [x] Replace crowded rows with adaptive spacing and a compact fallback.
- [x] Compare full and focused rendered views against the selected source.
- [x] Check compact route visibility and exit keys.

## Follow-up polish

P3: font size, dot shape and block continuity vary with the terminal's font and line spacing.
The capture is a terminal-cell rendering with synthetic data, not an operational task screenshot.
Native TUI interaction results are recorded separately in the testing and evidence documents.

final result: passed

## User-requested compact follow-up

The user reported bottom-only selection padding. Removed adaptive row gaps and the extra
highlighted blank line. Every conversation now occupies exactly one line; selected context follows
the inventory immediately with keyboard hints immediately underneath. The refreshed actual renderer
capture in `.runtime/graphite-screen.png` was opened at 100 by 40 with the same fixture.
This compact layout intentionally supersedes the original concept's vertical spacing.

final result: passed

The subsequent width correction caps the live panel at 96 columns and removes footer stretching.
The refreshed 100 by 40 renderer capture was inspected; the whole panel stays together.
