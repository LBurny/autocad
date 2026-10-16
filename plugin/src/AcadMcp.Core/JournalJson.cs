using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace AcadMcp.Core;

/// <summary>
/// Serialisation for the journal and its steps.
/// </summary>
/// <remarks>
/// Writing goes through the serialiser, because we control the data. Reading is
/// done by hand and is deliberately paranoid: the payload comes back out of a
/// drawing file that a person may have edited, truncated or hand-crafted, and a
/// journal that throws on load takes the whole palette down with it. Every field
/// is read with a default, an unparseable record is skipped, and nothing in here
/// ever raises.
/// </remarks>
public static class JournalJson
{
    public const int Version = 1;

    private static readonly JsonSerializerOptions WriteOptions = new()
    {
        WriteIndented = false,
        DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.Never,
    };

    // ---------------------------------------------------------------- writing

    public static string ToJson(Journal journal)
    {
        var records = new JsonArray();
        foreach (var record in journal.Records)
        {
            records.Add(JsonSerializer.SerializeToNode(record, WriteOptions));
        }

        var root = new JsonObject
        {
            ["version"] = Version,
            ["description"] = journal.Description,
            ["records"] = records,
        };
        return root.ToJsonString(WriteOptions);
    }

    public static JsonObject RecordToNode(StepRecord record) =>
        JsonSerializer.SerializeToNode(record, WriteOptions) as JsonObject ?? new JsonObject();

    /// <summary>
    /// Primitive objects as a JSON array for the RPC wire, in the vocabulary the Python
    /// readers compare against.
    /// </summary>
    /// <remarks>
    /// <para>
    /// A step row handed to Python is a <em>view</em>, not the stored payload, and the one
    /// place the two differ is the kind: the stored journal writes the enum's own name
    /// ("Line", "Insert") and Python compares kinds literally — <c>kind != "text"</c>,
    /// <c>kind != "block"</c> — so a PascalCase kind read as none of them. That is the same
    /// one-vocabulary mistake that made a plugin undo report ok while erasing nothing. Every
    /// kind therefore goes out lower-cased, which is how the Python journal's own recorder
    /// writes them and how <c>dxf.py</c> names a placed block ("insert").
    /// </para>
    /// <para>
    /// Reading is unaffected either way: <see cref="TryReadKind"/> ignores case and
    /// punctuation, so both spellings load back as the kind they name.
    /// </para>
    /// </remarks>
    public static JsonArray PrimitivesForWire(IReadOnlyList<Primitive> primitives)
    {
        var array = new JsonArray();
        foreach (var primitive in primitives)
        {
            var node = JsonSerializer.SerializeToNode(primitive, WriteOptions) as JsonObject
                ?? new JsonObject();
            node["kind"] = primitive.Kind.ToString().ToLowerInvariant();
            array.Add(node);
        }
        return array;
    }

    // ---------------------------------------------------------------- reading

    /// <summary>Parse a journal, returning an empty one rather than throwing.</summary>
    public static Journal FromJson(string? json)
    {
        var journal = new Journal();
        if (string.IsNullOrWhiteSpace(json))
        {
            return journal;
        }

        JsonNode? root;
        try
        {
            root = JsonNode.Parse(json);
        }
        catch (JsonException)
        {
            return journal;
        }

        if (root is not JsonObject obj)
        {
            return journal;
        }

        // A version this build does not know is refused rather than read with defaults.
        // Every field reader here is tolerant, which is right for a hand-edited file and
        // exactly wrong for a *newer* one: a renamed or re-typed field would load as its
        // default and the step would look intact while meaning something else.
        if (Int(obj, "version", Version) > Version)
        {
            return journal;
        }

        journal.Description = Str(obj, "description");

        if (obj["records"] is not JsonArray array)
        {
            return journal;
        }

        foreach (var node in array)
        {
            if (node is not JsonObject item)
            {
                continue;
            }
            var record = ReadRecord(item);
            if (record is not null)
            {
                journal.AddExecuted(record);
            }
        }
        return journal;
    }

    public static StepRecord? RecordFromNode(JsonNode? node) =>
        node is JsonObject obj ? ReadRecord(obj) : null;

    /// <summary>
    /// Append step payloads in journal order, returning how many could not be read.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Indices are assigned by <em>position</em>, not taken from the payload. That matters
    /// in one case: a drawing whose middle record was lost — a truncated chunk chain, or a
    /// hand-edited dictionary — otherwise loads into a list whose positions are contiguous
    /// while the stored numbers have a hole in them. <see cref="Journal.At"/> is positional,
    /// so from that point on the step number shown on screen and the step a caller can act
    /// on would be different steps. Numbering by position keeps the two in step, and it is
    /// what a save already does anyway.
    /// </para>
    /// <para>
    /// The unreadable count is returned rather than swallowed, because those steps are gone
    /// for good: their geometry is still in the drawing and nothing owns it any more.
    /// </para>
    /// </remarks>
    public static int AppendOrdered(Journal journal, IEnumerable<string?> payloads)
    {
        var unreadable = 0;
        foreach (var payload in payloads)
        {
            var record = string.IsNullOrWhiteSpace(payload) ? null : RecordFromJson(payload!);
            if (record is null)
            {
                unreadable++;
                continue;
            }
            // AddExecuted numbers by position, which is the whole point of going this way.
            journal.AddExecuted(record);
        }
        return unreadable;
    }

    private static StepRecord? RecordFromJson(string json)
    {
        try
        {
            return JsonNode.Parse(json) is JsonObject obj ? ReadRecord(obj) : null;
        }
        catch (Exception)
        {
            // Unreadable is the same as absent as far as a caller is concerned.
            return null;
        }
    }

    private static StepRecord? ReadRecord(JsonObject o)
    {
        try
        {
            var primitives = new List<Primitive>();
            if (o["primitives"] is JsonArray prims)
            {
                foreach (var p in prims)
                {
                    if (p is JsonObject po && ReadPrimitive(po) is { } primitive)
                    {
                        primitives.Add(primitive);
                    }
                }
            }

            JsonObject? parameters = null;
            if (o["params"] is JsonObject po2)
            {
                parameters = (JsonObject)po2.DeepClone();
            }

            return new StepRecord
            {
                Index = Int(o, "index"),
                State = ReadState(Str(o, "state")),
                Op = Str(o, "op"),
                Label = Str(o, "label"),
                Note = Str(o, "note"),
                Result = Str(o, "result"),
                Error = Str(o, "error"),
                Transaction = Str(o, "transaction"),
                Mutated = Bool(o, "mutated", true),
                Atomic = Bool(o, "atomic", true),
                Executable = Bool(o, "executable", true),
                Accepted = Bool(o, "accepted"),
                Drifted = Bool(o, "drifted"),
                ObjectsBefore = Strings(o["objects_before"]),
                ObjectsAfter = Strings(o["objects_after"]),
                Params = parameters,
                Primitives = primitives,
                Timestamp = ReadTime(Str(o, "timestamp")),
                DurationMs = Long(o, "duration_ms"),
            };
        }
        catch (Exception)
        {
            // One bad record must not cost the other ninety-nine.
            return null;
        }
    }

    private static Primitive? ReadPrimitive(JsonObject o)
    {
        try
        {
            var name = Str(o, "kind");
            var kind = TryReadKind(name);
            var color = o["color"] is JsonValue cv && cv.TryGetValue<int>(out var c) ? c : (int?)null;
            var linetype = Str(o, "linetype");
            return new Primitive
            {
                Kind = kind ?? PrimitiveKind.Unknown,
                // The word is kept only when it named nothing: a primitive this build can
                // draw has no use for it, and carrying it would make the payload noisier
                // with every round trip.
                UnknownKind = kind is null
                    ? (name.Length > 0 ? name : NullableStr(o, "unknown_kind"))
                    : null,
                Points = Numbers(o["points"]),
                Closed = Bool(o, "closed"),
                Radius = Num(o, "radius"),
                StartAngle = Num(o, "start_angle"),
                EndAngle = Num(o, "end_angle"),
                Text = Str(o, "text"),
                Height = Num(o, "height"),
                Width = Num(o, "width"),
                Rotation = Num(o, "rotation"),
                Align = Str(o, "align", "left"),
                Layer = Str(o, "layer", "0"),
                Linetype = string.IsNullOrWhiteSpace(linetype) ? null : linetype,
                Pattern = Str(o, "pattern", "ANSI31"),
                Scale = Num(o, "scale", 1.0),
                Angle = Num(o, "angle"),
                Solid = Bool(o, "solid"),
                Color = color,
                Offset = Num(o, "offset"),
                TextHeight = Num(o, "text_height"),
                Block = Str(o, "block"),
                Source = NullableStr(o, "source"),
                ScaleX = Num(o, "scale_x", 1.0),
                ScaleY = Num(o, "scale_y", 1.0),
                Properties = o["properties"] is JsonObject props ? (JsonObject)props.DeepClone() : null,
            };
        }
        catch (Exception)
        {
            return null;
        }
    }

    // ---------------------------------------------------------------- converters

    /// <summary>Case- and punctuation-insensitive kind lookup, so "MText" and "mtext" both land.</summary>
    /// <remarks>
    /// A name that matches nothing reads as <see cref="PrimitiveKind.Unknown"/>, which the
    /// builder refuses. It used to read as <see cref="PrimitiveKind.Line"/>, so a kind this
    /// build has no verb for — a sheet's <c>viewport</c>, or a typo — was drawn as a
    /// zero-length line at the origin and journalled as a success.
    /// </remarks>
    public static PrimitiveKind ReadKind(string raw) => TryReadKind(raw) ?? PrimitiveKind.Unknown;

    /// <summary>The kind a name names, or null when it names none of them.</summary>
    public static PrimitiveKind? TryReadKind(string raw)
    {
        var key = Normalise(raw);
        if (key.Length == 0)
        {
            return null;
        }
        foreach (var value in Enum.GetValues<PrimitiveKind>())
        {
            if (value != PrimitiveKind.Unknown && Normalise(value.ToString()) == key)
            {
                return value;
            }
        }
        return null;
    }

    public static StepState ReadState(string raw)
    {
        var key = Normalise(raw);
        foreach (var value in Enum.GetValues<StepState>())
        {
            if (Normalise(value.ToString()) == key)
            {
                return value;
            }
        }
        // A record with no usable state is treated as applied: assuming the
        // opposite would let a rollback erase entities the drawing still needs.
        return StepState.Done;
    }

    private static string Normalise(string value) =>
        new(value.Where(char.IsLetterOrDigit).Select(char.ToLowerInvariant).ToArray());

    // ---------------------------------------------------------------- field readers

    private static string Str(JsonObject o, string key, string fallback = "")
    {
        if (o[key] is JsonValue v && v.GetValueKind() == JsonValueKind.String)
        {
            return v.GetValue<string>();
        }
        return fallback;
    }

    /// <summary>Like <see cref="Str"/>, but an absent or empty string means "unset" rather than "".</summary>
    private static string? NullableStr(JsonObject o, string key)
    {
        var value = Str(o, key);
        return value.Length == 0 ? null : value;
    }

    private static bool Bool(JsonObject o, string key, bool fallback = false)
    {
        if (o[key] is JsonValue v)
        {
            if (v.GetValueKind() == JsonValueKind.True)
            {
                return true;
            }
            if (v.GetValueKind() == JsonValueKind.False)
            {
                return false;
            }
            if (v.GetValueKind() == JsonValueKind.String && bool.TryParse(v.GetValue<string>(), out var parsed))
            {
                return parsed;
            }
        }
        return fallback;
    }

    private static double Num(JsonObject o, string key, double fallback = 0)
    {
        if (o[key] is JsonValue v && v.TryGetValue<double>(out var d) && !double.IsNaN(d))
        {
            return d;
        }
        return fallback;
    }

    private static int Int(JsonObject o, string key, int fallback = 0) =>
        (int)Num(o, key, fallback);

    private static long Long(JsonObject o, string key, long fallback = 0) =>
        (long)Num(o, key, fallback);

    private static IReadOnlyList<double> Numbers(JsonNode? node)
    {
        var result = new List<double>();
        if (node is JsonArray array)
        {
            foreach (var item in array)
            {
                if (item is JsonValue v && v.TryGetValue<double>(out var d) && !double.IsNaN(d))
                {
                    result.Add(d);
                }
            }
        }
        return result;
    }

    private static IReadOnlyList<string> Strings(JsonNode? node)
    {
        var result = new List<string>();
        if (node is JsonArray array)
        {
            foreach (var item in array)
            {
                if (item is JsonValue v && v.GetValueKind() == JsonValueKind.String)
                {
                    result.Add(v.GetValue<string>());
                }
            }
        }
        return result;
    }

    private static DateTime ReadTime(string raw) =>
        DateTime.TryParse(raw, CultureInfo.InvariantCulture, DateTimeStyles.None, out var parsed)
            ? parsed
            : default;
}
