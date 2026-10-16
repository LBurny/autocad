using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace AcadMcp.Core;

/// <summary>Where a step is in its life. A failed step leaves nothing behind.</summary>
[JsonConverter(typeof(JsonStringEnumConverter))]
public enum StepState
{
    /// <summary>Not applied yet — waiting for the run cursor.</summary>
    Planned,

    /// <summary>Applied and present in the drawing.</summary>
    Done,

    /// <summary>Attempted, aborted, so its effect is not in the drawing.</summary>
    Failed,
}

/// <summary>One step of the plan: either a promise or a record of what happened.</summary>
public sealed record StepRecord
{
    [JsonPropertyName("index")]
    public int Index { get; init; }

    [JsonPropertyName("state")]
    public StepState State { get; init; } = StepState.Done;

    /// <summary>The drawing verb that produced this step, e.g. "draw_room".</summary>
    [JsonPropertyName("op")]
    public string Op { get; init; } = "";

    [JsonPropertyName("label")]
    public string Label { get; init; } = "";

    /// <summary>Free-text note; the user can also use it to explain a manual edit.</summary>
    [JsonPropertyName("note")]
    public string Note { get; init; } = "";

    [JsonPropertyName("result")]
    public string Result { get; init; } = "";

    [JsonPropertyName("error")]
    public string Error { get; init; } = "";

    /// <summary>AutoCAD undo-entry name this step committed, or "" when it owns none.</summary>
    [JsonPropertyName("transaction")]
    public string Transaction { get; init; } = "";

    /// <summary>False for a record that provably changed nothing.</summary>
    [JsonPropertyName("mutated")]
    public bool Mutated { get; init; } = true;

    /// <summary>False when the step owns no single undo entry, so erase-by-handle is the only safe revert.</summary>
    [JsonPropertyName("atomic")]
    public bool Atomic { get; init; } = true;

    /// <summary>False when the step cannot be re-created from its primitives.</summary>
    [JsonPropertyName("executable")]
    public bool Executable { get; init; } = true;

    /// <summary>Reviewed by the user. A soft lock: rolling back across it needs force.</summary>
    [JsonPropertyName("accepted")]
    public bool Accepted { get; init; }

    /// <summary>Set when the drawing drifted from this step since it ran.</summary>
    [JsonPropertyName("drifted")]
    public bool Drifted { get; init; }

    [JsonPropertyName("objects_before")]
    public IReadOnlyList<string> ObjectsBefore { get; init; } = [];

    [JsonPropertyName("objects_after")]
    public IReadOnlyList<string> ObjectsAfter { get; init; } = [];

    /// <summary>The arguments the caller gave. Descriptive; the MCP can re-derive from these.</summary>
    [JsonPropertyName("params")]
    public JsonObject? Params { get; init; }

    /// <summary>The resolved geometry. Executable; the plugin replays from these.</summary>
    [JsonPropertyName("primitives")]
    public IReadOnlyList<Primitive> Primitives { get; init; } = [];

    [JsonPropertyName("timestamp")]
    public DateTime Timestamp { get; init; }

    [JsonPropertyName("duration_ms")]
    public long DurationMs { get; init; }

    /// <summary>Handles this step introduced, in drawing order.</summary>
    [JsonIgnore]
    public IReadOnlyList<string> Handles => ObjectsAfter;

    [JsonIgnore]
    public bool IsApplied => State == StepState.Done;

    /// <summary>
    /// Whether the user is allowed to run this step right now. Failed steps count:
    /// their transaction aborted, so the drawing is as if they had never run.
    /// </summary>
    [JsonIgnore]
    public bool IsRunnable => State is StepState.Planned or StepState.Failed;

    /// <summary>A done step that owns no undo entry cannot be undone, only re-created.</summary>
    /// <remarks>
    /// A step that provably changed nothing is recoverable whatever its <see cref="Executable"/>
    /// flag says, because there is nothing to re-create — a baseline snapshot is exactly that
    /// case, and it is stored <c>Executable = false</c> on purpose. Testing <c>Mutated &amp;&amp;
    /// Executable</c> made every rollback past a snapshot report a step that "cannot be
    /// re-created", which is a warning about a step that never drew anything.
    /// </remarks>
    [JsonIgnore]
    public bool IsRecoverable => State != StepState.Done || !Mutated || Executable;

    /// <summary>One row of the steps list: index, name and a state marker.</summary>
    public string RowText() => $"{Index,3}  {(Drifted ? "~" : StateMark())}  {DisplayName()}";

    /// <summary>A single character summarising the state, for a text-only view.</summary>
    public string StateMark() => State switch
    {
        StepState.Done => Accepted ? "*" : "+",
        StepState.Failed => "x",
        _ => ".",
    };

    /// <summary>The best available name for this step.</summary>
    public string DisplayName() =>
        !string.IsNullOrWhiteSpace(Label) ? Label
        : !string.IsNullOrWhiteSpace(Op) ? Op
        : "step";

    /// <summary>Full description for a tooltip: state, op, counts and any error.</summary>
    public string TooltipText()
    {
        var lines = new List<string>
        {
            $"#{Index}  {DisplayName()}",
            $"state: {State.ToString().ToLowerInvariant()}"
                + (Accepted ? " (accepted)" : "")
                + (Drifted ? " (drifted from the drawing)" : ""),
        };
        if (!string.IsNullOrWhiteSpace(Op))
        {
            lines.Add($"op: {Op}");
        }
        if (Primitives.Count > 0)
        {
            lines.Add($"primitives: {Primitives.Count}");
        }
        if (ObjectsAfter.Count > 0)
        {
            lines.Add($"handles: {ObjectsAfter.Count}");
        }
        if (!string.IsNullOrWhiteSpace(Note))
        {
            lines.Add($"note: {Note}");
        }
        if (!string.IsNullOrWhiteSpace(Error))
        {
            lines.Add($"error: {Error}");
        }
        return string.Join(Environment.NewLine, lines);
    }
}
