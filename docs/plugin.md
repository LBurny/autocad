# The AutoCAD plugin

A .NET 8 plugin that lives inside AutoCAD and keeps a **step journal inside the
drawing**. Each step is created in one transaction together with its journal entry,
so a step can be rolled back as a unit, replayed later, and reviewed by the user in
a docked palette — with or without the MCP server running.

The geometry still comes from this repository's Python: `geom.py` and `ops.py` decide
where everything goes, and the plugin is handed already-resolved primitives (line,
polyline, text, mtext, hatch, circle, arc, dimension). The plugin creates them,
records their handles, and keeps the log. That split is why a plan drawn through the
plugin has the same coordinates as one drawn through the `com` or `dxf` backends.

## Install

```bash
plugin/scripts/install.sh            # refuses to touch a running AutoCAD
plugin/scripts/install.sh --force    # close it first
plugin/scripts/install.sh --uninstall
```

It needs the .NET 8 **SDK** and AutoCAD 2026 (release R25.1). Everything lands in
per-user locations, so no administrator is needed:

- the bundle goes to `%APPDATA%\Autodesk\ApplicationPlugins\AcadMcp.bundle`
- that folder is added to AutoCAD's `TRUSTEDPATHS`

The trust step is not cosmetic. With `SECURELOAD=1` — the stock setting, and this
machine's — an unsigned DLL in a folder AutoCAD does not trust raises a *Security -
Unsigned Executable* prompt, and "Always Load" does not settle it: every rebuild
produces a new unsigned file, so the prompt returns each time. While it is up the
plugin never loads, which looks exactly like a plugin that is not installed.

The setting lives in AutoCAD's profile, which AutoCAD writes back to the registry as
it exits — so it has to be written while AutoCAD is closed, or it is silently
overwritten on shutdown.

Then start AutoCAD; the plugin loads itself. Type `ACADMCPHELLO` to open the palette,
or let the AI open it.

## Using it from AutoCAD

The palette (`ACADMCPHELLO`) shows the journal for the active drawing:

| Row of buttons | What they do |
|---|---|
| Run next / Run all / Roll back / Run to | Move through the plan. "Roll back" erases everything after the selected step and sets those steps back to *planned* |
| Accept / Reject | Accept marks a step reviewed, so a later rollback will not pass it by accident. Reject drops a step and everything after it |
| Baseline / Refresh | Baseline records a reviewed checkpoint; Refresh re-reads the journal from the drawing |

Below the list, the **Step** tab shows what the selected step holds — its state, op,
transaction id, the primitives it drew, its entity handles — and an editable JSON
patch for its params, `label` or `note`.

The palette calls the engine in process and re-reads the journal from the drawing
every second, so it keeps working with the MCP server stopped, and a `Ctrl+Z` in
AutoCAD is reflected in the panel rather than being hidden by it.

## Using it from the AI client

The plugin backend records primitives instead of drawing them, so a step is opened
and closed around ordinary drawing tools:

```
plugin_connect(new_document=True)                       # draw through the plugin
step_begin("draw_room", label="clean room",
           params={"boundary": [[0,0],[4000,0],[4000,3000],[0,3000]]})
draw_room(boundary=[[0,0],[4000,0],[4000,3000],[0,3000]], name="无菌室", thickness=200)
step_commit()                                           # → one step, applied
```

`step_commit(planned=True)` records the step **without drawing it**, which is how a
whole plan is laid out first and then drawn a step at a time — by the user pressing
*Run next*, or by `step_control("run_next")`.

| Tool | Purpose |
|---|---|
| `plugin_status` | Is the plugin there, what drawing, what is in the journal. Also distinguishes "AutoCAD is busy" from "not loaded" |
| `plugin_connect` | Make the plugin the session backend. `new_document=True` opens a fresh drawing |
| `plugin_install` | Run the installer (checkout only) |
| `step_begin` / `step_commit` | Bracket the drawing calls that make one step |
| `step_list` | The journal rows |
| `step_control` | `run_next`, `run_all`, `run_to`, `rollback_to`, `reject`, `accept`, `unaccept`, `note`, `update`, `snapshot`, `clear_plan`, `reset`, `reload` |

`rollback_to` is refused when an accepted step stands in the way; `force=True`
overrides that, and the result says it did.

## Architecture

```
ZCode ──MCP/stdio──▶ acadmcp (Python)
                       │  PluginBackend: records primitives, sends whole steps
                       │  StepSession: drives the journal
                       ▼  TCP 127.0.0.1:9880, one JSON object per line
             AutoCAD 2026 process
               AcadMcp.bundle (autoloaded, ApplicationPlugins)
                 AcadMcp.Core     journal model, rollback arithmetic — no AutoCAD reference
                 AcadMcp.Plugin   StepEngine, JournalStore, PrimitiveBuilder, TextStyles,
                                  StepPanel + StepsControl, SelfTest
                 AcadMcp.Plugin.Rpc  RpcServer, IdleQueue, DrawingApi
```

**Storage.** The journal is a dictionary (`ACADMCP`) in the drawing's named object
dictionary, one Xrecord per step, chunked so a step with many primitives still fits.
It is a database object, so it is saved with the drawing, travels with the file, and
participates in undo.

**Rollback erases handles.** AutoCAD's .NET API offers no way to name an undo record,
so the "named transaction checked against the undo stack" approach is not available
here. Instead a rollback erases the entities the steps above the target created,
looked up by handle. That is deterministic, independent of how deep the undo stack
is, and undisturbed by anything the user typed in between. The guard against erasing
the wrong thing is that the set of handles to erase is computed as a *set difference*:
anything a surviving step also owns is never included.

**One rollback is one undo record.** The erases and the journal write happen in the
same transaction, so `Ctrl+Z` puts the drawing and the log back together rather than
leaving the log claiming a step is applied while its entities are gone. This is
measured, not assumed: the in-AutoCAD self-test rolls back, then presses `Ctrl+Z` (one
`_.UNDO 1` on the command line, with no command active, which is how the user's
Ctrl+Z arrives) and checks that all six entities come back *and* that the journal's
handle lists come back with them.

**Interaction protection.** Everything that touches the database runs on AutoCAD's
main thread, dispatched through `IdleQueue` from `Application.Idle`. Idle also fires
while a command is waiting for input, so the queue checks `Command.InProgress` and
waits its turn instead of slipping edits inside somebody else's command. Idle does
*not* fire while a modal dialog is up, so a request made then cannot run at all: it
is given a bounded wait and then told what was in the way, and it is marked abandoned
so a timed-out call can never execute later. This replaces the COM backend's
`RPC_E_CALL_REJECTED` retry loop with something that can explain itself.

## Wire protocol

Loopback only, `ACADMCP_PORT` or 9880. One JSON object per line in, one per line out.

```json
{"id": 3, "method": "rollback_to", "params": {"index": 2, "force": false}}
{"id": 3, "ok": true, "result": {"ok": true, "action": "rollback_to", "message": "..."},
 "status": {"count": 3, "applied": 3, ...}}
```

- `ping` is answered on the socket thread **without touching AutoCAD**, so it works
  while AutoCAD is blocked. That is the point of it.
- `{"ok": false, "blocked": true, "method": "...", "error": "AutoCAD is in the LINE
  command ..."}` means the plugin was there but could not run the call in time.

Methods: `ping`, `status`, `info`, `get_steps`, `reload`, `apply`, `plan`, `run_next`,
`run_all`, `run_to`, `rollback_to`, `reject`, `accept`, `note`, `snapshot`,
`clear_plan`, `reset`, `update`, `ensure_layer`, `set_units_mm`, `entities`,
`new_document`.

The port is unauthenticated: any process on this machine can drive the drawing. That
is the same trust level the AutoCAD instance already has, and binding to loopback is
what keeps it off the network.

## Persistence, and why it is deliberately simple

The journal lives in the drawing: a dictionary (`ACADMCP`) in the named object
dictionary, one Xrecord per step (`S0001`, chunked into `S0001.1`, `.2` … at 4000
characters), plus a `meta` record with the version, the plan's description and the
count. It travels with the .dwg, and because an Xrecord is a database object,
`Ctrl+Z` reverts the journal and the geometry together.

Two properties of the file format are worth knowing before touching it:

- **A save rewrites every record.** That is a deliberate trade: a writer that only
  writes what changed has to be told what changed, and it can be told wrong — a
  missed record is a silent, permanent loss. Rewriting everything cannot drift. The
  cost was measured rather than assumed, see below.
- **Step numbers are positions.** A record that cannot be read is counted
  (`journal.Unreadable`, shown in the palette and in `plugin_status`) and the
  survivors are renumbered. Numbering by position is what keeps `Journal.At`, which
  is positional, in step with the number shown on the row — a damaged load used to be
  able to leave those two disagreeing, so that asking for step 4 could hand back
  step 5.

### The measured numbers

From the in-AutoCAD self-test (`[note] scale at …`), on this machine:

| Journal length | Cost of one more step | Full re-read | One journal-only change |
|---|---|---|---|
| 200 steps | 4.1 ms | 9 ms | 8 ms |
| 400 steps | 10.9 ms | 11 ms | 9 ms |

The growth with length is in the *geometry* path — entity creation and the undo
record that carries it — not in the journal write: a journal-only change stays flat
at 8–9 ms as the journal doubles. So writing the journal incrementally would not fix
the part that grows, and a full re-read costs ~1% of one core per second even at 400
steps, which is what the palette's poll does.

**Conclusion, recorded so it is not re-litigated:** the obvious optimisations —
incremental journal writes, and a revision counter so a re-read can skip parsing —
were planned, measured and dropped. Neither is worth the risk of a writer that can
be told the wrong thing. Revisit if drawings with thousands of steps turn up, and
re-measure first: `dotnet test` plus `scripts/selftest.sh --engine` prints the numbers.

## What is not done yet

- **Manual-edit drift is not detected.** If the user edits or erases a step's geometry
  by hand, the journal does not notice and does not mark the step drifted. Rollback
  and replay still do the right thing — the handles are still the handles — but the
  panel will not warn that a step no longer matches what it drew.
- **The plugin backend cannot reframe the view.** `screenshot` captures the live
  AutoCAD window; zooming to a window is refused rather than silently ignored.
- **The plugin backend does not save.** Saving the drawing the user has open is their
  call; use AutoCAD's own save.
- **`update` accepts a patch, not a re-run.** Changing a step's params changes the
  record; to apply the change to the geometry, roll back to it and run again.

## Testing

```bash
cd plugin
dotnet test                                  # journal arithmetic, no AutoCAD
./scripts/selftest.sh                        # both suites in one scratch AutoCAD, ~25s
./scripts/selftest.sh --engine --shot p.png  # and photograph the palette
```

- **engine** — the plugin tests itself inside a real AutoCAD at startup
  (`ACADMCP_SELFTEST=1`): apply, the CJK text style, journal round-trip through the
  DWG, rollback, replay, the accepted-step refusal, the `Ctrl+Z` interaction, and that
  the palette opens. Report in `%TEMP%\acadmcp-plugin.log`, ending `AUTO-SELFTEST-DONE`.
- **rpc** — `tests/live_rpc_check.py` drives the running plugin over the real socket
  using this repository's own client, backend, journal control and MCP tools.

`tools/AcadMcp.ApiProbe` dumps AutoCAD API signatures by reading the assemblies as
metadata with AutoCAD closed. It exists because a loaded .NET assembly cannot be
unloaded from AutoCAD, so a wrong API guess would otherwise cost a restart each time.
