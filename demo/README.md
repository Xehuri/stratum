# Stratum Demo

This directory is a synthetic demonstration corpus for Stratum.

It contains no real personal data, names, locations, health records, or private notes. All entities, measurements, and events are fictional.

The demo is intended to exercise:

- multiple workspaces
- heading-based segmentation
- a custom profile
- a manual line range
- a fenced code block that contains Markdown-looking headings
- a Markdown table
- a legacy narrative document
- a contract-compliant atomic claim document
- a CJK/UTF-8 fixture

Run the corpus with `stratum demo` from the repository root. The runner copies
the synthetic workspaces to a temporary directory, never writes `.stratum/`
into this tree, and prints deterministic plain text. `demo/expected/` describes
the output; `--keep` preserves the temporary directory for inspection.
