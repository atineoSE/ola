# Helper Scripts

Development and troubleshooting utilities for `ola-top`.

Since `ola-top` is an interactive TUI, it can't be tested by running it directly in a non-interactive context. These scripts let you inspect rendering output without a live terminal.

## Usage

All scripts must be run from the project root with `PYTHONPATH=src`:

```bash
PYTHONPATH=src python helper-scripts/<script>.py
```

## Scripts

### render_preview.py

Renders the `ola-top` table to stdout using fixture data. Useful for iterating on layout, column widths, colors, and styling without launching the full TUI.

Outputs three views:
- **Collapsed** — all folders collapsed, cursor on first row
- **Expanded** — one folder expanded to show iteration sub-rows
- **Narrow terminal** — 60-column width to check truncation behavior

Edit the `FIXTURES` list at the top of the file to test different data scenarios (e.g. many folders, long names, zero tokens).

### test_live_render.py

Verifies that Rich's `Live` widget correctly overwrites previous frames during refresh cycles. Captures the raw ANSI escape sequences and checks that each frame transition emits enough cursor-up (`\x1b[1A`) sequences to fully clear the prior frame.

Output:
- **OK** — cursor-up counts match, no duplicate headers will appear
- **FAIL** — mismatch detected, stale rows will be visible on screen

Run this after changing terminal mode settings (`tty.setcbreak`, `tty.setraw`) or Rich `Live` configuration to catch rendering regressions.
