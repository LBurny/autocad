using System.Text.Json.Nodes;

namespace AcadMcp.Core;

/// <summary>
/// The step journal: what has been drawn, what is only planned, and what
/// reverting any of it would involve.
/// </summary>
/// <remarks>
/// Deliberately free of AutoCAD types so it can be unit-tested without a running
/// AutoCAD — the same split CADPilot uses for its FreeCAD journal. The AutoCAD
/// half (transactions, handle erasure, Xrecord storage) lives in the plugin
/// project and treats this class as the single source of truth for the
/// arithmetic.
///
/// Step indices are <b>1-based</b> and match the list position, so index 0 means
/// "before every step" and reads naturally as a rollback target.
/// </remarks>
public sealed class Journal
{
    private readonly List<StepRecord> _records = [];

    /// <summary>What the plan is for, shown in the panel header.</summary>
    public string Description { get; set; } = "";

    public IReadOnlyList<StepRecord> Records => _records;

    public int Count => _records.Count;

    /// <summary>The applied steps, in order.</summary>
    public IEnumerable<StepRecord> Applied => _records.Where(r => r.IsApplied);

    public StepRecord? At(int index) =>
        index >= 1 && index <= _records.Count ? _records[index - 1] : null;

    public bool IsEmpty => _records.Count == 0;

    // ---------------------------------------------------------------- planning

    /// <summary>
    /// Replace the un-run tail of the plan with <paramref name="steps"/>.
    /// </summary>
    /// <remarks>
    /// Only the tail: a plan cannot rewrite history. Anything already applied, or
    /// failed, stays exactly as it is, and the new steps are appended after them
    /// with fresh indices.
    /// </remarks>
    public IReadOnlyList<StepRecord> SetPlan(string description, IEnumerable<StepRecord> steps)
    {
        if (!string.IsNullOrWhiteSpace(description))
        {
            Description = description;
        }
        DropPlanned();

        var added = new List<StepRecord>();
        foreach (var step in steps)
        {
            var record = step with
            {
                Index = _records.Count + 1,
                State = StepState.Planned,
                Error = "",
                Accepted = false,
                Drifted = false,
                ObjectsBefore = [],
                ObjectsAfter = [],
                Transaction = "",
                Timestamp = DateTime.Now,
            };
            _records.Add(record);
            added.Add(record);
        }
        return added;
    }

    /// <summary>Drop planned steps. Never touches the drawing.</summary>
    public int DropPlanned()
    {
        var removed = _records.RemoveAll(r => r.State == StepState.Planned);
        Reindex();
        return removed;
    }

    /// <summary>Append a step that has already been applied.</summary>
    public StepRecord AddExecuted(StepRecord record)
    {
        var stored = record with { Index = _records.Count + 1 };
        _records.Add(stored);
        return stored;
    }

    /// <summary>The next step the run cursor should execute, or null when done.</summary>
    /// <remarks>
    /// Failed steps are runnable: their transaction aborted, so the drawing is as
    /// if they had never run and re-running is the natural retry.
    /// </remarks>
    public StepRecord? NextRunnable() => _records.FirstOrDefault(r => r.IsRunnable);

    /// <summary>Indices of every step that still needs running.</summary>
    public IReadOnlyList<int> PendingIndices() =>
        _records.Where(r => r.IsRunnable).Select(r => r.Index).ToList();

    // ---------------------------------------------------------------- history

    /// <summary>
    /// Handles introduced by steps after <paramref name="toIndex"/> that no
    /// surviving step also created.
    /// </summary>
    /// <remarks>
    /// The subtraction is the point. A step's handle list is only meaningful as a
    /// list of things <i>that step</i> made; erasing everything after an index
    /// without checking what earlier steps own is how a rollback deletes the
    /// user's work.
    /// </remarks>
    public IReadOnlyList<string> CreatedSince(int toIndex)
    {
        var owned = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var record in _records.Where(r => r.Index <= toIndex))
        {
            foreach (var handle in record.ObjectsAfter)
            {
                owned.Add(handle);
            }
        }

        var doomed = new List<string>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var record in _records.Where(r => r.Index > toIndex))
        {
            foreach (var handle in record.ObjectsAfter)
            {
                if (!owned.Contains(handle) && seen.Add(handle))
                {
                    doomed.Add(handle);
                }
            }
        }
        return doomed;
    }

    /// <summary>Applied steps above the target that a rollback would revert.</summary>
    public IReadOnlyList<StepRecord> AffectedBy(int toIndex) =>
        _records.Where(r => r.Index > toIndex && r.IsApplied).ToList();

    /// <summary>Applied steps above the target that cannot be re-created if removed.</summary>
    public IReadOnlyList<StepRecord> UnrecoverableAbove(int toIndex) =>
        _records.Where(r => r.Index > toIndex && !r.IsRecoverable).ToList();

    /// <summary>
    /// Work out what rolling back to <paramref name="toIndex"/> would do, without
    /// doing it.
    /// </summary>
    public RollbackPlan PlanRollback(int toIndex, bool force = false)
    {
        if (toIndex < 0 || toIndex > _records.Count)
        {
            return new RollbackPlan
            {
                Allowed = false,
                TargetIndex = toIndex,
                Refusal = $"step {toIndex} does not exist; this drawing has {_records.Count} step(s)",
            };
        }

        var affected = AffectedBy(toIndex);
        if (affected.Count == 0)
        {
            return new RollbackPlan
            {
                Allowed = true,
                TargetIndex = toIndex,
                Refusal = "",
            };
        }

        var blocking = affected.Where(r => r.Mutated && !r.Atomic).Select(r => r.Index).ToList();
        var accepted = affected.Where(r => r.Accepted).Select(r => r.Index).ToList();

        if (!force && blocking.Count > 0)
        {
            return new RollbackPlan
            {
                Allowed = false,
                TargetIndex = toIndex,
                Affected = affected.Select(r => r.Index).ToList(),
                Blocking = blocking,
                Accepted = accepted,
                Refusal = $"step(s) {Join(blocking)} changed the drawing without leaving a "
                    + "handle set to revert, so rolling past them could remove the wrong "
                    + "entities; pass force to proceed anyway",
            };
        }

        if (!force && accepted.Count > 0)
        {
            return new RollbackPlan
            {
                Allowed = false,
                TargetIndex = toIndex,
                Affected = affected.Select(r => r.Index).ToList(),
                Blocking = blocking,
                Accepted = accepted,
                Refusal = $"step(s) {Join(accepted)} are marked accepted; pass force to roll "
                    + "back across reviewed work",
            };
        }

        return new RollbackPlan
        {
            Allowed = true,
            TargetIndex = toIndex,
            Affected = affected.Select(r => r.Index).ToList(),
            Blocking = blocking,
            Accepted = accepted,
            Unrecoverable = UnrecoverableAbove(toIndex).Select(r => r.Index).ToList(),
            HandlesToErase = CreatedSince(toIndex),
        };
    }

    /// <summary>
    /// Demote every applied step above the target back to planned, and failed
    /// steps too, so the run cursor will pick them up again.
    /// </summary>
    /// <remarks>
    /// This is the second half of a rollback, and the first half of a rebuild:
    /// the caller erases the handles, then calls this, then runs the now-planned
    /// steps again. That is why there is no separate rebuild mode — replaying is
    /// the rebuild.
    /// </remarks>
    public IReadOnlyList<int> Rewind(int toIndex)
    {
        var touched = new List<int>();
        for (var i = 0; i < _records.Count; i++)
        {
            var record = _records[i];
            if (record.Index <= toIndex || record.State == StepState.Planned)
            {
                continue;
            }
            _records[i] = record with
            {
                State = StepState.Planned,
                Transaction = "",
                ObjectsBefore = [],
                ObjectsAfter = [],
                Error = "",
                Drifted = false,
                Accepted = false,
            };
            touched.Add(record.Index);
        }
        return touched;
    }

    /// <summary>
    /// Remove everything from <paramref name="index"/> onward and report the
    /// handles that must go with them.
    /// </summary>
    /// <remarks>
    /// Reject is the deliberate destructive act, so it ignores the accepted
    /// soft-lock; the records are about to stop existing, which is the point.
    /// </remarks>
    public (IReadOnlyList<string> Handles, IReadOnlyList<int> Removed) PlanReject(int index)
    {
        var doomed = _records.Where(r => r.Index >= index).ToList();
        var survivors = _records.Where(r => r.Index < index).ToList();

        var owned = new HashSet<string>(
            survivors.SelectMany(r => r.ObjectsAfter), StringComparer.OrdinalIgnoreCase);
        var handles = new List<string>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var record in doomed)
        {
            foreach (var handle in record.ObjectsAfter)
            {
                if (!owned.Contains(handle) && seen.Add(handle))
                {
                    handles.Add(handle);
                }
            }
        }

        var removed = doomed.Select(r => r.Index).ToList();
        _records.RemoveRange(index - 1, _records.Count - (index - 1));
        return (handles, removed);
    }

    // ---------------------------------------------------------------- edits

    /// <summary>
    /// Shallow-merge <paramref name="patch"/> into a step's params. A patch of
    /// <c>label</c> alone renames the step instead, which works on any state.
    /// </summary>
    public StepRecord? Update(int index, JsonObject patch)
    {
        var record = At(index);
        if (record is null)
        {
            return null;
        }

        var replacement = record;
        if (patch.TryGetPropertyValue("label", out var label) && label is not null)
        {
            replacement = replacement with { Label = label.GetValue<string>() };
            patch.Remove("label");
        }
        if (patch.TryGetPropertyValue("note", out var note) && note is not null)
        {
            replacement = replacement with { Note = note.GetValue<string>() };
            patch.Remove("note");
        }
        if (patch.Count > 0)
        {
            var merged = record.Params is null ? new JsonObject() : (JsonObject)record.Params.DeepClone();
            foreach (var pair in patch)
            {
                merged[pair.Key] = pair.Value?.DeepClone();
            }
            replacement = replacement with { Params = merged };
        }

        _records[index - 1] = replacement;
        return replacement;
    }

    /// <summary>
    /// Swap a record for a new version of itself, keeping its index canonical.
    /// </summary>
    /// <remarks>
    /// Used when a step changes state — planned to done, or to failed — where the
    /// caller has built the whole replacement. The index always wins over
    /// whatever the incoming record claims, so a record can never end up filed
    /// under a step it is not.
    /// </remarks>
    public StepRecord? Replace(int index, StepRecord record)
    {
        if (index < 1 || index > _records.Count)
        {
            return null;
        }
        var stored = record with { Index = index };
        _records[index - 1] = stored;
        return stored;
    }

    /// <summary>Mark a step reviewed, or clear the mark.</summary>
    public StepRecord? SetAccepted(int index, bool accepted)
    {
        var record = At(index);
        if (record is null)
        {
            return null;
        }
        var replacement = record with { Accepted = accepted };
        _records[index - 1] = replacement;
        return replacement;
    }

    /// <summary>Attach a free-text note, e.g. to explain an off-journal manual edit.</summary>
    public StepRecord? SetNote(int index, string note)
    {
        var record = At(index);
        if (record is null)
        {
            return null;
        }
        var replacement = record with { Note = note };
        _records[index - 1] = replacement;
        return replacement;
    }

    /// <summary>Flag that the drawing no longer matches what a step produced.</summary>
    public StepRecord? MarkDrifted(int index, bool drifted = true)
    {
        var record = At(index);
        if (record is null || record.Drifted == drifted)
        {
            return record;
        }
        var replacement = record with { Drifted = drifted };
        _records[index - 1] = replacement;
        return replacement;
    }

    /// <summary>Replace a step's stored geometry, as a manual edit sync does.</summary>
    public StepRecord? ReplacePrimitives(int index, IReadOnlyList<Primitive> primitives)
    {
        var record = At(index);
        if (record is null)
        {
            return null;
        }
        var replacement = record with { Primitives = primitives };
        _records[index - 1] = replacement;
        return replacement;
    }

    /// <summary>How many applied steps own no usable undo entry.</summary>
    public IReadOnlyList<int> StepsWithoutUndo() =>
        _records
            .Where(r => r.IsApplied && r.Transaction.Length == 0 && r.Mutated)
            .Select(r => r.Index)
            .ToList();

    /// <summary>
    /// The newest applied step that carries a transaction — the anchor for
    /// telling whether the drawing still matches the journal.
    /// </summary>
    /// <remarks>
    /// Deliberately not "the newest applied step": a trailing record that owns no
    /// undo entry would make any staleness check report clean.
    /// </remarks>
    public StepRecord? LastAtomicDone() =>
        _records.LastOrDefault(r => r.IsApplied && r.Atomic && r.Mutated && r.Transaction.Length > 0);

    /// <summary>Every entity handle the journal claims to have created.</summary>
    public IReadOnlyCollection<string> TrackedHandles() =>
        _records
            .SelectMany(r => r.ObjectsAfter)
            .ToHashSet(StringComparer.OrdinalIgnoreCase);

    /// <summary>Which step owns a handle, for manual-edit sync.</summary>
    public int? OwnerOf(string handle)
    {
        for (var i = _records.Count - 1; i >= 0; i--)
        {
            if (_records[i].ObjectsAfter.Contains(handle, StringComparer.OrdinalIgnoreCase))
            {
                return _records[i].Index;
            }
        }
        return null;
    }

    /// <summary>One-line-per-state counts for a header or a tool reply.</summary>
    public string Summary()
    {
        int done = 0, planned = 0, failed = 0, accepted = 0, drifted = 0;
        foreach (var record in _records)
        {
            switch (record.State)
            {
                case StepState.Done:
                    done++;
                    break;
                case StepState.Failed:
                    failed++;
                    break;
                default:
                    planned++;
                    break;
            }
            if (record.Accepted)
            {
                accepted++;
            }
            if (record.Drifted)
            {
                drifted++;
            }
        }

        var parts = new List<string> { $"{done} done" };
        if (planned > 0)
        {
            parts.Add($"{planned} planned");
        }
        if (failed > 0)
        {
            parts.Add($"{failed} failed");
        }
        if (accepted > 0)
        {
            parts.Add($"{accepted} accepted");
        }
        if (drifted > 0)
        {
            parts.Add($"{drifted} drifted");
        }
        return string.Join(", ", parts);
    }

    /// <summary>Build a planned step from an operation and its arguments.</summary>
    public static StepRecord Planned(
        string op,
        JsonObject? parameters = null,
        string? label = null,
        IReadOnlyList<Primitive>? primitives = null) =>
        new()
        {
            Op = op,
            Label = label ?? Describe(op, parameters),
            Params = parameters,
            Primitives = primitives ?? [],
            State = StepState.Planned,
        };

    /// <summary>
    /// A readable name for a step: the op plus the caller's own name for what it
    /// draws, when the arguments carry one.
    /// </summary>
    public static string Describe(string op, JsonObject? parameters)
    {
        if (parameters is null)
        {
            return op;
        }
        foreach (var key in (string[])["name", "label", "tag", "title"])
        {
            if (parameters.TryGetPropertyValue(key, out var value)
                && value is not null
                && value.GetValueKind() == System.Text.Json.JsonValueKind.String)
            {
                var text = value.GetValue<string>();
                if (!string.IsNullOrWhiteSpace(text))
                {
                    return $"{op} {text}";
                }
            }
        }
        return op;
    }

    private void Reindex()
    {
        for (var i = 0; i < _records.Count; i++)
        {
            if (_records[i].Index != i + 1)
            {
                _records[i] = _records[i] with { Index = i + 1 };
            }
        }
    }

    private static string Join(IEnumerable<int> indices) => string.Join(", ", indices);
}
