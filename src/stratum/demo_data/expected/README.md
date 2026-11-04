# Expected Demo Output

`stratum demo` runs the synthetic corpus in a temporary directory. The runner
prints plain text only, never JSON.

The output is deterministic. Temporary absolute paths and generated timestamps
are not part of the default output. `--keep` preserves the temporary directory
and prints its path, which is the only volatile line in that mode.

The expected output covers:

- workspace discovery and per-workspace block counts
- a heading-selected block
- a table inside a structured claim block
- a fenced code block containing heading-looking text that is not split
- a manual line range with a stable block id
- a prose paragraph hit
- a legacy narrative hit
- one promoted card and one promoted L2 claim
- a clean fsck run
- one linear commit and one log entry per workspace
- an active claim query

Golden files under `tests/golden/` capture this output after normalising the
temporary directory path, generated timestamps and the random fsck run id.
