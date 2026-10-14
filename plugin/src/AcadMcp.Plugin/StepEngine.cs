using System.Diagnostics;
using System.Globalization;
using System.Text.Json.Nodes;
using AcadMcp.Core;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;

namespace AcadMcp.Plugin;

/// <summary>The result of one engine call, shaped for the RPC layer and the panel.</summary>
public sealed record StepOutcome
{
    public bool Ok { get; init; }

    public string Action { get; init; } = "";

    /// <summary>What happened, in words the user can read.</summary>
    public string Message { get; init; } = "";

    /// <summary>Step indices this call changed.</summary>
    public IReadOnlyList<int> Touched { get; init; } = [];

    /// <summary>Set when the call was refused and the caller could override it.</summary>
    public string? Refusal { get; init; }

    public static StepOutcome Fail(string action, string message, string? refusal = null) =>
        new() { Ok = false, Action = action, Message = message, Refusal = refusal };
}

/// <summary>
/// Applies steps to a drawing and reverts them.
/// </summary>
/// <remarks>
/// <para>
/// Reverting happens by <b>erasing the handles a step created</b>, not by popping
/// AutoCAD's undo stack. That is not a shortcut: AutoCAD's .NET API offers no way
/// to name an undo record, so the "named transaction verified against the undo
/// stack" approach CADPilot uses on FreeCAD has no equivalent here — see
/// <see cref="Transaction"/> on the record, which is our own id rather than an
/// AutoCAD entry name.
/// </para>
/// <para>
/// Erasing by handle is also the more robust half of the trade: it works
/// whatever the undo stack's depth, and it is not disturbed by the user having
/// typed a few commands in between. The guard against deleting the wrong thing
/// is that <see cref="Journal.CreatedSince"/> subtracts what surviving steps
/// own, so a rollback can only ever remove entities this journal created.
/// </para>
/// <para>
/// Every mutation writes the journal inside the same transaction as the
/// geometry. Because an Xrecord is a database object, <c>Ctrl+Z</c> then reverts
/// both together and the journal cannot drift out of step with the drawing.
/// </para>
/// </remarks>
public sealed class StepEngine
{
    private static readonly Dictionary<Document, StepEngine> Cache = [];

    private readonly Document _doc;
    private readonly Database _db;
    private readonly JournalStore _store;

    private Journal _journal = new();
    private long _sequence;

    private StepEngine(Document document)
    {
        _doc = document;
        _db = document.Database;
        _store = new JournalStore(_db);
        Reload();
    }

    /// <summary>The engine for a document, created on first use and reused after.</summary>
    public static StepEngine For(Document document)
    {
        lock (Cache)
        {
            if (!Cache.TryGetValue(document, out var engine))
            {
                engine = new StepEngine(document);
                Cache[document] = engine;
            }
            return engine;
        }
    }

    /// <summary>Forget a closed document's engine.</summary>
    public static void Forget(Document document)
    {
        lock (Cache)
        {
            Cache.Remove(document);
        }
    }

    public Document Document => _doc;

    public Journal Journal => _journal;

    /// <summary>Re-read the journal from the drawing, discarding in-memory state.</summary>
    public Journal Reload()
    {
        using var docLock = _doc.LockDocument();
        using var tr = _db.TransactionManager.StartTransaction();
        _journal = _store.Load(tr);
        tr.Commit();

        // Keep our id sequence ahead of anything already in the drawing, so a
        // reopened file never reuses a step's transaction id.
        _sequence = Math.Max(_sequence, _journal.Count);
        return _journal;
    }

    // ---------------------------------------------------------------- applying

    /// <summary>Apply a fresh step and journal it.</summary>
    public StepOutcome Apply(
        string op,
        string? label,
        JsonObject? arguments,
        IReadOnlyList<Primitive> primitives)
    {
        var started = Stopwatch.StartNew();
        var plan = new StepRecord
        {
            Op = op,
            Label = string.IsNullOrWhiteSpace(label) ? Journal.Describe(op, arguments) : label,
            Params = arguments,
            Primitives = primitives,
            State = StepState.Done,
            ObjectsBefore = [],
            Timestamp = DateTime.Now,
        };

        try
        {
            using var docLock = _doc.LockDocument();
            using var tr = _db.TransactionManager.StartTransaction();
            var handles = Create(tr, primitives);

            var record = plan with
            {
                Index = _journal.Count + 1,
                ObjectsAfter = handles,
                Transaction = NextTransactionId(),
                DurationMs = started.ElapsedMilliseconds,
            };
            _journal.AddExecuted(record);
            _store.Save(tr, _journal);
            tr.Commit();

            return new StepOutcome
            {
                Ok = true,
                Action = "apply",
                Touched = [record.Index],
                Message = $"step {record.Index} {record.DisplayName()}: "
                    + $"{handles.Count} entity/ies",
            };
        }
        catch (System.Exception error)
        {
            // The abort rolled the journal write back with the geometry, so the
            // in-memory copy is now ahead of the drawing. Re-read before
            // recording the failure, or the next step gets the wrong index.
            Reload();
            RecordFailure(plan, error, started.ElapsedMilliseconds);
            PluginLog.Write($"step {plan.Op} failed", error);
            return StepOutcome.Fail("apply", $"step failed: {error.Message}");
        }
    }

    /// <summary>Run the next step that still needs running.</summary>
    public StepOutcome RunNext()
    {
        var next = _journal.NextRunnable();
        return next is null
            ? StepOutcome.Fail("run_next", "nothing left to run")
            : Execute(next);
    }

    /// <summary>Run every remaining step, stopping at the first failure.</summary>
    public StepOutcome RunAll()
    {
        var ran = new List<int>();
        while (_journal.NextRunnable() is { } next)
        {
            var outcome = Execute(next);
            ran.Add(next.Index);
            if (!outcome.Ok)
            {
                return new StepOutcome
                {
                    Ok = false,
                    Action = "run_all",
                    Touched = ran,
                    Message = $"stopped at step {next.Index}: {outcome.Message}",
                };
            }
        }
        return new StepOutcome
        {
            Ok = true,
            Action = "run_all",
            Touched = ran,
            Message = ran.Count == 0 ? "nothing left to run" : $"ran {ran.Count} step(s)",
        };
    }

    /// <summary>Run until the cursor has passed <paramref name="index"/>.</summary>
    public StepOutcome RunTo(int index)
    {
        var ran = new List<int>();
        while (_journal.NextRunnable() is { } next && next.Index <= index)
        {
            var outcome = Execute(next);
            ran.Add(next.Index);
            if (!outcome.Ok)
            {
                return new StepOutcome
                {
                    Ok = false,
                    Action = "run_to",
                    Touched = ran,
                    Message = $"stopped at step {next.Index}: {outcome.Message}",
                };
            }
        }
        return new StepOutcome
        {
            Ok = true,
            Action = "run_to",
            Touched = ran,
            Message = ran.Count == 0 ? $"nothing to run up to step {index}" : $"ran {ran.Count} step(s)",
        };
    }

    /// <summary>Re-create a planned or failed step from its stored geometry.</summary>
    private StepOutcome Execute(StepRecord planned)
    {
        var started = Stopwatch.StartNew();
        if (!planned.Executable)
        {
            // Nothing to re-create; mark it done so the cursor moves past it
            // rather than retrying it forever.
            using var docLock = _doc.LockDocument();
            using var tr = _db.TransactionManager.StartTransaction();
            Replace(planned with
            {
                State = StepState.Done,
                Mutated = false,
                Atomic = false,
                Transaction = "",
                ObjectsAfter = [],
                Result = "skipped: not re-executable",
            });
            _store.Save(tr, _journal);
            tr.Commit();
            return new StepOutcome
            {
                Ok = true,
                Action = "run",
                Touched = [planned.Index],
                Message = $"step {planned.Index} skipped: not re-executable",
            };
        }

        try
        {
            using var docLock = _doc.LockDocument();
            using var tr = _db.TransactionManager.StartTransaction();
            var handles = Create(tr, planned.Primitives);

            var record = planned with
            {
                State = StepState.Done,
                ObjectsBefore = [],
                ObjectsAfter = handles,
                Transaction = NextTransactionId(),
                Error = "",
                Drifted = false,
                DurationMs = started.ElapsedMilliseconds,
            };
            Replace(record);
            _store.Save(tr, _journal);
            tr.Commit();

            return new StepOutcome
            {
                Ok = true,
                Action = "run",
                Touched = [record.Index],
                Message = $"step {record.Index} {record.DisplayName()}: {handles.Count} entity/ies",
            };
        }
        catch (System.Exception error)
        {
            Reload();
            Fail(planned.Index, error, started.ElapsedMilliseconds);
            PluginLog.Write($"step {planned.Index} failed", error);
            return StepOutcome.Fail("run", $"step {planned.Index} failed: {error.Message}");
        }
    }

    // ---------------------------------------------------------------- reverting

    /// <summary>Erase everything above a step and put those steps back to planned.</summary>
    public StepOutcome RollbackTo(int index, bool force = false)
    {
        var plan = _journal.PlanRollback(index, force);
        if (!plan.Allowed)
        {
            return StepOutcome.Fail("rollback_to", plan.Refusal, plan.Refusal);
        }
        if (plan.Affected.Count == 0)
        {
            return new StepOutcome
            {
                Ok = true,
                Action = "rollback_to",
                Message = $"nothing to revert above step {index}",
            };
        }

        try
        {
            using var docLock = _doc.LockDocument();
            using var tr = _db.TransactionManager.StartTransaction();

            var erased = Erase(tr, plan.HandlesToErase);
            var rewound = _journal.Rewind(index);
            _store.Save(tr, _journal);
            tr.Commit();

            LogRollback(plan, erased);
            return new StepOutcome
            {
                Ok = true,
                Action = "rollback_to",
                Touched = rewound,
                Message = $"reverted {rewound.Count} step(s), erased {erased.Count} entity/ies"
                    + (plan.Partial
                        ? $"; step(s) {string.Join(", ", plan.Unrecoverable)} cannot be re-created"
                        : ""),
            };
        }
        catch (System.Exception error)
        {
            Reload();
            PluginLog.Write($"rollback to {index} failed", error);
            return StepOutcome.Fail("rollback_to", $"rollback failed: {error.Message}");
        }
    }

    /// <summary>Erase a step and everything after it, and drop them from the journal.</summary>
    public StepOutcome Reject(int index)
    {
        try
        {
            var plan = _journal.PlanReject(index);
            using var docLock = _doc.LockDocument();
            using var tr = _db.TransactionManager.StartTransaction();

            var erased = Erase(tr, plan.Handles);
            _store.Save(tr, _journal);
            tr.Commit();

            return new StepOutcome
            {
                Ok = true,
                Action = "reject",
                Touched = plan.Removed,
                Message = $"dropped step(s) {string.Join(", ", plan.Removed)}, "
                    + $"erased {erased.Count} entity/ies",
            };
        }
        catch (System.Exception error)
        {
            Reload();
            PluginLog.Write($"reject {index} failed", error);
            return StepOutcome.Fail("reject", $"reject failed: {error.Message}");
        }
    }

    // ---------------------------------------------------------------- plan edits

    /// <summary>Replace the un-run tail of the plan.</summary>
    public StepOutcome SetPlan(string description, IEnumerable<StepRecord> steps)
    {
        try
        {
            var added = _journal.SetPlan(description, steps);
            Persist();
            return new StepOutcome
            {
                Ok = true,
                Action = "set_plan",
                Touched = added.Select(r => r.Index).ToList(),
                Message = $"planned {added.Count} step(s)",
            };
        }
        catch (System.Exception error)
        {
            Reload();
            return StepOutcome.Fail("set_plan", error.Message);
        }
    }

    public StepOutcome Update(int index, JsonObject patch)
    {
        try
        {
            var record = _journal.Update(index, patch);
            if (record is null)
            {
                return StepOutcome.Fail("update", $"no step {index}");
            }
            Persist();
            return new StepOutcome
            {
                Ok = true,
                Action = "update",
                Touched = [index],
                Message = $"step {index} updated",
            };
        }
        catch (System.Exception error)
        {
            Reload();
            return StepOutcome.Fail("update", error.Message);
        }
    }

    public StepOutcome SetAccepted(int index, bool accepted)
    {
        try
        {
            if (_journal.SetAccepted(index, accepted) is null)
            {
                return StepOutcome.Fail("accept", $"no step {index}");
            }
            Persist();
            return new StepOutcome
            {
                Ok = true,
                Action = "accept",
                Touched = [index],
                Message = $"step {index} {(accepted ? "accepted" : "no longer accepted")}",
            };
        }
        catch (System.Exception error)
        {
            Reload();
            return StepOutcome.Fail("accept", error.Message);
        }
    }

    public StepOutcome SetNote(int index, string note)
    {
        try
        {
            if (_journal.SetNote(index, note) is null)
            {
                return StepOutcome.Fail("note", $"no step {index}");
            }
            Persist();
            return new StepOutcome
            {
                Ok = true,
                Action = "note",
                Touched = [index],
                Message = $"step {index} annotated",
            };
        }
        catch (System.Exception error)
        {
            Reload();
            return StepOutcome.Fail("note", error.Message);
        }
    }

    /// <summary>
    /// Record a baseline: the drawing is as it should be here, and anything a
    /// person changed by hand up to now is accepted.
    /// </summary>
    public StepOutcome Snapshot(string note)
    {
        try
        {
            // Not executable and not mutated: it owns no geometry and must never
            // be treated as something a rollback has to re-create.
            var record = new StepRecord
            {
                State = StepState.Done,
                Op = "snapshot",
                Label = string.IsNullOrWhiteSpace(note) ? "snapshot" : $"snapshot: {note}",
                Note = note,
                Mutated = false,
                Atomic = false,
                Executable = false,
                Accepted = true,
                ObjectsBefore = [],
                ObjectsAfter = [],
                Timestamp = DateTime.Now,
            };
            _journal.AddExecuted(record);
            Persist();
            return new StepOutcome
            {
                Ok = true,
                Action = "snapshot",
                Touched = [record.Index],
                Message = $"baseline recorded at step {record.Index}",
            };
        }
        catch (System.Exception error)
        {
            Reload();
            return StepOutcome.Fail("snapshot", error.Message);
        }
    }

    public StepOutcome ClearPlan()
    {
        try
        {
            var dropped = _journal.DropPlanned();
            Persist();
            return new StepOutcome
            {
                Ok = true,
                Action = "clear_plan",
                Message = $"dropped {dropped} planned step(s)",
            };
        }
        catch (System.Exception error)
        {
            Reload();
            return StepOutcome.Fail("clear_plan", error.Message);
        }
    }

    /// <summary>Forget the journal entirely. Touches no geometry.</summary>
    public StepOutcome Reset()
    {
        try
        {
            using var docLock = _doc.LockDocument();
            using var tr = _db.TransactionManager.StartTransaction();
            _store.SaveEmpty(tr);
            tr.Commit();
            Reload();
            return new StepOutcome { Ok = true, Action = "reset", Message = "journal cleared" };
        }
        catch (System.Exception error)
        {
            Reload();
            return StepOutcome.Fail("reset", error.Message);
        }
    }

    // ---------------------------------------------------------------- internals

    /// <summary>Create primitives in the current space and report their handles.</summary>
    private List<string> Create(Transaction tr, IReadOnlyList<Primitive> primitives)
    {
        var blockTable = (BlockTable)tr.GetObject(_db.BlockTableId, OpenMode.ForRead);
        var model = (BlockTableRecord)tr.GetObject(
            blockTable[BlockTableRecord.ModelSpace], OpenMode.ForWrite);

        var handles = new List<string>();
        foreach (var primitive in primitives)
        {
            EnsureLayer(tr, primitive.Layer);
            EnsureLinetype(tr, primitive.Linetype);

            var entity = PrimitiveBuilder.Build(primitive, _db);
            model.AppendEntity(entity);
            tr.AddNewlyCreatedDBObject(entity, true);
            PrimitiveBuilder.Finish(entity);
            handles.Add(entity.Handle.ToString());
        }
        return handles;
    }

    /// <summary>Erase entities by handle, skipping anything already gone.</summary>
    private List<string> Erase(Transaction tr, IReadOnlyList<string> handles)
    {
        var erased = new List<string>();
        foreach (var text in handles)
        {
            if (!long.TryParse(text, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var value))
            {
                continue;
            }
            if (!_db.TryGetObjectId(new Handle(value), out var id) || id.IsNull || !id.IsValid)
            {
                // Already deleted by the user or by an earlier edit: nothing to do,
                // and not an error worth failing the rollback over.
                continue;
            }

            var obj = tr.GetObject(id, OpenMode.ForWrite, openErased: false, forceOpenOnLockedLayer: false);
            if (obj is null || obj.IsErased)
            {
                continue;
            }
            obj.Erase();
            erased.Add(text);
        }
        return erased;
    }

    private void EnsureLayer(Transaction tr, string name)
    {
        if (string.IsNullOrWhiteSpace(name))
        {
            return;
        }
        var table = (LayerTable)tr.GetObject(_db.LayerTableId, OpenMode.ForRead);
        if (table.Has(name))
        {
            return;
        }
        table.UpgradeOpen();
        var layer = new LayerTableRecord { Name = name };
        table.Add(layer);
        tr.AddNewlyCreatedDBObject(layer, true);
    }

    private void EnsureLinetype(Transaction tr, string? name)
    {
        if (string.IsNullOrWhiteSpace(name) || name == "CONTINUOUS")
        {
            return;
        }
        var table = (LinetypeTable)tr.GetObject(_db.LinetypeTableId, OpenMode.ForRead);
        if (table.Has(name))
        {
            return;
        }
        try
        {
            _db.LoadLineTypeFile(name, "acad.lin");
        }
        catch (System.Exception)
        {
            // Not in the standard library. The linetype is cosmetic, so the
            // geometry is better off drawn than refused.
        }
    }

    private string NextTransactionId() => $"acadmcp#{++_sequence}";

    /// <summary>Swap a record for a new version of itself.</summary>
    private void Replace(StepRecord record) => _journal.Replace(record.Index, record);

    private void RecordFailure(StepRecord plan, System.Exception error, long elapsedMs)
    {
        var record = plan with
        {
            Index = _journal.Count + 1,
            State = StepState.Failed,
            Error = error.Message,
            Transaction = "",
            ObjectsAfter = [],
            DurationMs = elapsedMs,
        };
        _journal.AddExecuted(record);
        Persist();
    }

    /// <summary>
    /// Mark a step failed. The abort destroyed whatever it did, so any handle it
    /// had claimed is cleared: keeping a stale list would make a later rollback
    /// try to erase entities that no longer exist, or worse, that something else
    /// now owns.
    /// </summary>
    private void Fail(int index, System.Exception error, long elapsedMs)
    {
        if (_journal.At(index) is not { } record)
        {
            return;
        }
        Replace(record with
        {
            State = StepState.Failed,
            Error = error.Message,
            Transaction = "",
            ObjectsBefore = [],
            ObjectsAfter = [],
            DurationMs = elapsedMs,
        });
        Persist();
    }

    /// <summary>Write the journal in its own transaction.</summary>
    private void Persist()
    {
        using var docLock = _doc.LockDocument();
        using var tr = _db.TransactionManager.StartTransaction();
        _store.Save(tr, _journal);
        tr.Commit();
    }

    private static void LogRollback(RollbackPlan plan, IReadOnlyList<string> erased)
    {
        PluginLog.Write(
            $"rollback to {plan.TargetIndex}: affected {plan.Affected.Count}, erased {erased.Count}"
            + (plan.Partial ? $", partial (unrecoverable {string.Join(",", plan.Unrecoverable)})" : ""));
    }
}
