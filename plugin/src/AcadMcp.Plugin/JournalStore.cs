using System.Text;
using System.Text.Json.Nodes;
using AcadMcp.Core;
using Autodesk.AutoCAD.DatabaseServices;

namespace AcadMcp.Plugin;

/// <summary>
/// Keeps the step journal inside the drawing, as Xrecords in a dictionary in the
/// named object dictionary.
/// </summary>
/// <remarks>
/// Storing it on the drawing rather than beside it is what lets the steps panel
/// keep working with the MCP server stopped, and it means the journal travels
/// with the .dwg when it is copied or emailed.
///
/// AutoCAD-specific bonus over the FreeCAD design this is modelled on: an
/// Xrecord is a database object, so <c>Ctrl+Z</c> reverts the journal and the
/// geometry together, and no reconciliation is needed.
///
/// One Xrecord per step, keyed <c>S0001</c>, chunked into <c>S0001.1</c>,
/// <c>S0001.2</c>… when a single step's JSON is long. Reading walks the suffixes
/// until one is missing, so the record is self-describing and no separate index
/// has to be kept in step with it.
/// </remarks>
public sealed class JournalStore
{
    private const string DictionaryName = "ACADMCP";
    private const string MetaKey = "meta";
    private const string StepPrefix = "S";

    /// <summary>
    /// Characters of JSON per Xrecord. Well under any DXF string limit, and large
    /// enough that an ordinary step is a single record.
    /// </summary>
    private const int ChunkChars = 4000;

    private readonly Database _db;

    public JournalStore(Database database) => _db = database;

    /// <summary>
    /// The journal dictionary: opened for writing when it may be created or
    /// extended, and strictly read-only otherwise.
    /// </summary>
    /// <remarks>
    /// The read-only path matters. Opening a dictionary <c>ForWrite</c> turns a
    /// plain read into a database write, and AutoCAD answers that with
    /// eLockViolation unless the caller holds a document lock — which a reader
    /// has no reason to take.
    /// </remarks>
    private DBDictionary OpenDictionary(Transaction tr, bool create)
    {
        if (create)
        {
            var writable = (DBDictionary)tr.GetObject(_db.NamedObjectsDictionaryId, OpenMode.ForRead);
            if (!writable.Contains(DictionaryName))
            {
                writable.UpgradeOpen();
                var created = new DBDictionary();
                writable.SetAt(DictionaryName, created);
                tr.AddNewlyCreatedDBObject(created, true);
                return created;
            }
            return (DBDictionary)tr.GetObject(writable.GetAt(DictionaryName), OpenMode.ForWrite);
        }

        var nod = (DBDictionary)tr.GetObject(_db.NamedObjectsDictionaryId, OpenMode.ForRead);
        if (!nod.Contains(DictionaryName))
        {
            throw new InvalidOperationException("no journal in this drawing");
        }
        return (DBDictionary)tr.GetObject(nod.GetAt(DictionaryName), OpenMode.ForRead);
    }

    /// <summary>True when the drawing already carries a journal.</summary>
    public bool Exists(Transaction tr)
    {
        var nod = (DBDictionary)tr.GetObject(_db.NamedObjectsDictionaryId, OpenMode.ForRead);
        return nod.Contains(DictionaryName);
    }

    /// <summary>Read the journal, or an empty one when the drawing has none.</summary>
    public Journal Load(Transaction tr)
    {
        var journal = new Journal();
        if (!Exists(tr))
        {
            return journal;
        }

        var dictionary = OpenDictionary(tr, create: false);
        var meta = ReadText(dictionary, tr, MetaKey);
        var version = JournalJson.Version;
        if (meta is not null)
        {
            try
            {
                if (JsonNode.Parse(meta) is JsonObject metaObject)
                {
                    journal.Description = metaObject["description"]?.GetValue<string>() ?? "";
                    version = metaObject["version"]?.GetValue<int>() ?? JournalJson.Version;
                }
            }
            catch (Exception)
            {
                // A corrupt meta block costs the header, not the steps: the records below
                // are self-describing, so the header is all that is lost.
            }
        }

        // A journal written by a newer build is not read at all. Every field reader in
        // JournalJson is tolerant by design, which means a renamed or re-typed field would
        // load as its default: the steps would come back looking whole while describing
        // something this build cannot interpret. Saying so is better than pretending.
        var bound = HighestStepIndex(dictionary);
        if (version > JournalJson.Version)
        {
            PluginLog.Write(
                $"journal: this drawing's journal is version {version} and this build writes "
                + $"{JournalJson.Version}; leaving its records up to step {bound} unread, because "
                + "a newer field would load as a default and the steps would read as intact");
            journal.Unreadable = bound;
            return journal;
        }

        // The walk is bounded by the highest step number present, not by the meta's `count`.
        // The count is a distinct-key total, so a journal with a hole — a truncated chunk
        // chain, a hand-edited dictionary — has a count smaller than its highest step number,
        // and reading 1..count would silently drop every step above the hole while their
        // geometry stayed in the drawing. A hand-edited count of two billion is the same bug
        // from the other side: it turned the loop into a multi-gigabyte allocation that no
        // reader here can catch.
        journal.Unreadable = JournalJson.AppendOrdered(journal, ReadPayloads(dictionary, tr, bound));
        if (journal.Unreadable > 0)
        {
            PluginLog.Write(
                $"journal: {journal.Unreadable} of {bound} record(s) could not be read; "
                + "those steps can neither be replayed nor rolled back, and their geometry "
                + "is now owned by nothing");
        }
        return journal;
    }

    private IEnumerable<string?> ReadPayloads(DBDictionary dictionary, Transaction tr, int bound)
    {
        for (var index = 1; index <= bound; index++)
        {
            yield return ReadStep(dictionary, tr, index);
        }
    }

    /// <summary>Write the whole journal, dropping any step records the journal no longer has.</summary>
    public void Save(Transaction tr, Journal journal)
    {
        var dictionary = OpenDictionary(tr, create: true);

        RemoveAllStepKeys(dictionary, tr);

        for (var index = 0; index < journal.Records.Count; index++)
        {
            var record = journal.Records[index] with { Index = index + 1 };
            WriteStep(dictionary, tr, record.Index, JournalJson.RecordToNode(record).ToJsonString());
        }

        var meta = new JsonObject
        {
            ["version"] = JournalJson.Version,
            ["description"] = journal.Description,
            ["count"] = journal.Records.Count,
        };
        WriteText(dictionary, tr, MetaKey, meta.ToJsonString());
    }

    /// <summary>Remove every record of a journal, leaving the dictionary itself in place.</summary>
    public void SaveEmpty(Transaction tr)
    {
        var dictionary = OpenDictionary(tr, create: true);
        RemoveAllStepKeys(dictionary, tr);
        var meta = new JsonObject { ["version"] = JournalJson.Version, ["description"] = "", ["count"] = 0 };
        WriteText(dictionary, tr, MetaKey, meta.ToJsonString());
    }

    // ------------------------------------------------------------ step records

    private void WriteStep(DBDictionary dictionary, Transaction tr, int index, string json)
    {
        var chunks = Chunk(json);
        for (var i = 0; i < chunks.Count; i++)
        {
            var key = i == 0 ? StepKey(index) : $"{StepKey(index)}.{i}";
            WriteText(dictionary, tr, key, chunks[i]);
        }
    }

    private string? ReadStep(DBDictionary dictionary, Transaction tr, int index)
    {
        var head = ReadText(dictionary, tr, StepKey(index));
        if (head is null)
        {
            return null;
        }

        var text = new StringBuilder(head);
        for (var i = 1; i < 512; i++)
        {
            var part = ReadText(dictionary, tr, $"{StepKey(index)}.{i}");
            if (part is null)
            {
                break;
            }
            text.Append(part);
        }
        return text.ToString();
    }

    private void RemoveAllStepKeys(DBDictionary dictionary, Transaction tr)
    {
        foreach (var key in StepKeys(dictionary).ToList())
        {
            var id = dictionary.GetAt(key);
            dictionary.Remove(key);
            Trash(tr, id);
        }
    }

    /// <summary>
    /// The highest step number present, ignoring chunk suffixes, or 0 for none.
    /// </summary>
    /// <remarks>
    /// This is the read walk's bound rather than the meta block's count, because it is the
    /// one number that cannot disagree with the records: a missing step in the middle must
    /// not truncate everything above it.
    /// </remarks>
    private static int HighestStepIndex(DBDictionary dictionary)
    {
        var highest = 0;
        foreach (var key in StepKeys(dictionary))
        {
            var head = key.Split('.')[0];
            if (int.TryParse(head.AsSpan(StepPrefix.Length), out var index) && index > highest)
            {
                highest = index;
            }
        }
        return highest;
    }

    private static IEnumerable<string> StepKeys(DBDictionary dictionary)
    {
        foreach (DBDictionaryEntry entry in dictionary)
        {
            var key = entry.Key;
            if (key.StartsWith(StepPrefix, StringComparison.Ordinal) && key.Length > StepPrefix.Length)
            {
                yield return key;
            }
        }
    }

    private static string StepKey(int index) =>
        $"{StepPrefix}{index:D4}";

    /// <summary>Split JSON into Xrecord-sized pieces without breaking a character.</summary>
    private static List<string> Chunk(string json)
    {
        if (json.Length <= ChunkChars)
        {
            return [json];
        }

        var chunks = new List<string>();
        // The advance is the length actually emitted, not the nominal chunk size: cutting a
        // surrogate pair short by one character and then stepping past it by the full chunk
        // drops that character entirely, which the guard above is there to prevent.
        for (var start = 0; start < json.Length;)
        {
            var length = Math.Min(ChunkChars, json.Length - start);
            // Never cut a surrogate pair in half: a lone half encodes to a
            // replacement character and the JSON stops parsing.
            if (start + length < json.Length && char.IsHighSurrogate(json[start + length - 1]))
            {
                length--;
            }
            chunks.Add(json.Substring(start, length));
            start += length;
        }
        return chunks;
    }

    // ------------------------------------------------------------ text records

    private static void WriteText(DBDictionary dictionary, Transaction tr, string key, string text)
    {
        var data = new ResultBuffer(new TypedValue((int)DxfCode.Text, text));
        if (dictionary.Contains(key))
        {
            var existing = (Xrecord)tr.GetObject(dictionary.GetAt(key), OpenMode.ForWrite);
            existing.Data = data;
            return;
        }

        var record = new Xrecord { Data = data };
        dictionary.SetAt(key, record);
        tr.AddNewlyCreatedDBObject(record, true);
    }

    private static string? ReadText(DBDictionary dictionary, Transaction tr, string key)
    {
        if (!dictionary.Contains(key))
        {
            return null;
        }
        var record = (Xrecord)tr.GetObject(dictionary.GetAt(key), OpenMode.ForRead);
        using var data = record.Data;
        if (data is null)
        {
            return null;
        }
        var text = new StringBuilder();
        foreach (TypedValue value in data)
        {
            if (value.Value is string piece)
            {
                text.Append(piece);
            }
        }
        return text.Length == 0 ? null : text.ToString();
    }

    private static void Trash(Transaction tr, ObjectId id)
    {
        // IsErased is checked before opening: asking to open an erased object with
        // openErased false raises eWasErased rather than answering null, so a stale id here
        // would abort the transaction instead of being skipped.
        if (id.IsNull || !id.IsValid || id.IsErased)
        {
            return;
        }
        var obj = tr.GetObject(id, OpenMode.ForWrite, openErased: false, forceOpenOnLockedLayer: false);
        if (obj is null || obj.IsErased)
        {
            return;
        }
        obj.Erase();
    }
}
