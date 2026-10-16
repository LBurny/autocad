# Evaluations

Fourteen read-only questions against this MCP server, in the format the mcp-builder
guide's harness (`evaluation.py`, copied here with its `connections.py`) runs.
`fixtures/floor_plan.dxf` is a checked-in copy of `examples/floor_plan_demo.py`'s
output, so the answers are stable; every answer was verified by driving the
tools directly before it was written down.

## Running

```bash
pip install -r evaluations/requirements.txt   # anthropic, mcp
set ANTHROPIC_API_KEY=...                      # or export, off Windows

# stdio: the harness launches the server itself
python evaluations/evaluation.py -t stdio -c uv -a run -a acadmcp evaluation.xml
```

Run from the repository root, so the relative fixture path in the questions
(`evaluations/fixtures/floor_plan.dxf`) resolves. The server does not need
AutoCAD for any of these questions — they all stay on the headless dxf backend
plus the element catalogue.

## Refreshing the fixture

```bash
uv run python examples/floor_plan_demo.py
cp examples/output/floor_plan.dxf evaluations/fixtures/floor_plan.dxf
```

Then re-verify every answer: the entity counts in `evaluation.xml` are the
demo's, and they change if the demo does.
