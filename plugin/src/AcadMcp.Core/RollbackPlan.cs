namespace AcadMcp.Core;

/// <summary>What a rollback would do, worked out before anything is touched.</summary>
/// <remarks>
/// Reverting an AutoCAD step means erasing the handles it created, so the plan
/// carries that handle set explicitly. It is computed as a set difference rather
/// than by replaying history, because the only unforgivable bug here is erasing
/// something an earlier, surviving step created.
/// </remarks>
public sealed record RollbackPlan
{
    public bool Allowed { get; init; }

    /// <summary>Why the rollback was refused, in words the user can act on.</summary>
    public string Refusal { get; init; } = "";

    /// <summary>Steps at or below this index are kept. 0 means "keep nothing".</summary>
    public int TargetIndex { get; init; }

    /// <summary>Applied steps that stop being applied.</summary>
    public IReadOnlyList<int> Affected { get; init; } = [];

    /// <summary>Affected steps whose change cannot be attributed to a handle set.</summary>
    public IReadOnlyList<int> Blocking { get; init; } = [];

    /// <summary>Affected steps the user has already reviewed.</summary>
    public IReadOnlyList<int> Accepted { get; init; } = [];

    /// <summary>Affected steps that cannot be re-created, so their loss is permanent.</summary>
    public IReadOnlyList<int> Unrecoverable { get; init; } = [];

    /// <summary>The exact entities to erase. Never includes anything an earlier step made.</summary>
    public IReadOnlyList<string> HandlesToErase { get; init; } = [];

    /// <summary>True when residue may remain because a step's handles are unknown.</summary>
    public bool Partial => Unrecoverable.Count > 0;

    public string Describe()
    {
        if (!Allowed)
        {
            return Refusal;
        }
        var text = $"revert {Affected.Count} step(s), erase {HandlesToErase.Count} entity/ies";
        if (Partial)
        {
            text += $" (partial: step(s) {string.Join(", ", Unrecoverable)} cannot be re-created)";
        }
        return text;
    }
}
