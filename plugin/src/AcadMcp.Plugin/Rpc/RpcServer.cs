using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using AcadMcp.Core;
using Autodesk.AutoCAD.ApplicationServices;
using AcadApp = Autodesk.AutoCAD.ApplicationServices.Core.Application;

namespace AcadMcp.Plugin;

/// <summary>
/// A line-delimited JSON socket that lets the MCP server drive this plugin.
/// </summary>
/// <remarks>
/// <para>
/// Loopback only. One JSON object per line in, one per line out, each response
/// carrying its request's <c>id</c> so a pipelining client can match them up.
/// </para>
/// <para>
/// <c>ping</c> is answered on the socket thread without touching AutoCAD at all.
/// That is the point of it: a client needs to tell "AutoCAD is busy" apart from
/// "the plugin is gone", and the one moment it must not hang is exactly when
/// AutoCAD is busy.
/// </para>
/// <para>
/// Everything else goes through <see cref="IdleQueue"/>, so it runs on AutoCAD's
/// main thread, and a request that cannot run in time is told why rather than
/// blocking forever.
/// </para>
/// <para>
/// The listener is unauthenticated: any process on this machine can drive the
/// drawing through it. That is the same trust level the AutoCAD instance itself
/// already has, and binding to <see cref="IPAddress.Loopback"/> is what keeps it
/// off the network — do not change that to <c>0.0.0.0</c>.
/// </para>
/// </remarks>
public sealed class RpcServer : IDisposable
{
    public const int DefaultPort = 9880;

    /// <summary>How long queued work may take before the caller is told what blocked it.</summary>
    private static readonly TimeSpan Timeout = ReadTimeout();

    /// <summary>A single line longer than this is refused rather than buffered.</summary>
    private const int MaxLineChars = 8 * 1024 * 1024;

    /// <summary>
    /// The most step rows one <c>get_steps</c> call may ask for.
    /// </summary>
    /// <remarks>
    /// This used to be 1000, which silently truncated: the Python side asks for 10000 meaning
    /// "the whole journal", so past a thousand steps the rows came back as the *oldest* prefix
    /// while the caller believed it held everything. `drawing_undo` then computed a target from
    /// the wrong rows and a one-step undo could erase hundreds. The bound is now generous
    /// enough for any real journal, the reply carries `total`, and the caller pages when it
    /// needs to.
    /// </remarks>
    private const int MaxStepsPerPage = 100_000;

    private readonly TcpListener _listener;
    private readonly CancellationTokenSource _stop = new();

    private RpcServer(TcpListener listener, int port)
    {
        _listener = listener;
        Port = port;
    }

    public int Port { get; }

    /// <summary>The listener this process started, if any.</summary>
    public static RpcServer? Current { get; private set; }

    /// <summary>Start listening, replacing any listener this process already has.</summary>
    public static RpcServer Start(int port)
    {
        Current?.Dispose();

        var listener = new TcpListener(IPAddress.Loopback, port);
        listener.Start();
        var server = new RpcServer(listener, port);
        Current = server;
        server.Listen();
        return server;
    }

    /// <summary>The port to bind: <c>ACADMCP_PORT</c>, else <see cref="DefaultPort"/>.</summary>
    public static int ConfiguredPort()
    {
        var raw = Environment.GetEnvironmentVariable("ACADMCP_PORT");
        return int.TryParse(raw, out var port) && port is > 0 and < 65536 ? port : DefaultPort;
    }

    private void Listen() => _ = Task.Run(AcceptLoopAsync);

    private async Task AcceptLoopAsync()
    {
        while (!_stop.IsCancellationRequested)
        {
            TcpClient client;
            try
            {
                client = await _listener.AcceptTcpClientAsync(_stop.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                break;
            }
            catch (ObjectDisposedException)
            {
                break;
            }
            catch (System.Exception error)
            {
                PluginLog.Write("rpc accept failed", error);
                continue;
            }
            _ = Task.Run(() => ServeAsync(client));
        }
    }

    private async Task ServeAsync(TcpClient client)
    {
        var encoding = new UTF8Encoding(false);
        try
        {
            using (client)
            using (var stream = client.GetStream())
            using (var reader = new StreamReader(stream, encoding, false, 4096, leaveOpen: true))
            using (var writer = new StreamWriter(stream, encoding, 4096, leaveOpen: true) { AutoFlush = true })
            {
                client.NoDelay = true;
                while (!_stop.IsCancellationRequested)
                {
                    var line = await reader.ReadLineAsync(_stop.Token).ConfigureAwait(false);
                    if (line is null)
                    {
                        return;
                    }
                    if (line.Length == 0)
                    {
                        continue;
                    }
                    var response = line.Length > MaxLineChars
                        ? Error(null, $"request too large ({line.Length} chars)")
                        : await DispatchAsync(line).ConfigureAwait(false);
                    await writer.WriteLineAsync(response).ConfigureAwait(false);
                }
            }
        }
        catch (OperationCanceledException)
        {
            // Shutting down.
        }
        catch (System.Exception error)
        {
            PluginLog.Write("rpc client ended", error);
        }
    }

    private async Task<string> DispatchAsync(string line)
    {
        JsonNode? request;
        try
        {
            request = JsonNode.Parse(line);
        }
        catch (JsonException error)
        {
            return Error(null, $"not valid JSON: {error.Message}");
        }

        if (request is not JsonObject call)
        {
            return Error(null, "a request must be a JSON object");
        }

        // Sanitised once here rather than trusted at each of the five places that echo it
        // back, and every branch below sits inside the catch. Both matter for the same
        // failure: an id holding an unpaired UTF-16 surrogate — which any client that
        // truncates a string mid-pair produces, and `json.dumps` will happily escape onto the
        // wire — throws when the reply tries to serialise it, so the ping branch, the
        // "needs a method" branch and the catch handlers themselves all raised *outside* the
        // old try. Nothing wrote a reply line at all, and `ServeAsync` closed the socket, so
        // the client's diagnosis was "the plugin is gone" — on `ping`, the one call whose
        // whole purpose is to tell those two apart. For a drawing call it was worse: the step
        // had already run and been journalled before the reply was built.
        var id = EchoableId(call["id"]);
        try
        {
            return await HandleAsync(call, id).ConfigureAwait(false);
        }
        catch (System.Exception error)
        {
            PluginLog.Write("rpc request failed", error);
            return Error(id, $"{error.GetType().Name}: {error.Message}");
        }
    }

    /// <summary>
    /// The request's id as a value a reply can carry, or null when it cannot be carried.
    /// </summary>
    /// <remarks>
    /// A clone rather than the node itself: constructing the clone is what forces the value
    /// to be read, so a value that survives this is one that will survive being written into
    /// a reply. A null id is a reply the client can still match up by its absence, which is a
    /// far better answer than no line at all.
    /// </remarks>
    private static JsonNode? EchoableId(JsonNode? id)
    {
        if (id is null)
        {
            return null;
        }
        try
        {
            return id.DeepClone();
        }
        catch (System.Exception)
        {
            return null;
        }
    }

    private async Task<string> HandleAsync(JsonObject call, JsonNode? id)
    {
        var method = call["method"] is JsonValue value && value.TryGetValue<string>(out var name)
            ? name
            : "";
        if (method.Length == 0)
        {
            return Error(id, "a request needs a method");
        }
        var parameters = call["params"] as JsonObject ?? new JsonObject();

        // Answered here and now, without the queue and without AutoCAD: ping has
        // to work while the application is blocked, or it cannot do its job.
        if (method == "ping")
        {
            return Ok(id, new JsonObject
            {
                ["pong"] = true,
                ["port"] = Port,
                ["version"] = typeof(RpcServer).Assembly.GetName().Version?.ToString() ?? "0",
                ["queue"] = IdleQueue.Attached ? "attached" : "detached",
                ["waiting"] = IdleQueue.Depth,
            });
        }

        try
        {
            var result = await IdleQueue.RunAsync(
                () => Invoke(method, parameters), Timeout).ConfigureAwait(false);
            return Ok(id, result);
        }
        catch (IdleQueueBlockedException blocked)
        {
            return Blocked(id, method, blocked.Message);
        }
        catch (System.Exception error)
        {
            return Error(id, $"{error.GetType().Name}: {error.Message}");
        }
    }

    /// <summary>
    /// The method table. Runs on AutoCAD's main thread, which is why every
    /// AutoCAD call below is legal here and nowhere else.
    /// </summary>
    private JsonObject Invoke(string method, JsonObject p)
    {
        // Handled before the active document is resolved, because it is the call
        // that creates one: a freshly started AutoCAD has only its Start tab.
        if (method == "new_document")
        {
            return DrawingApi.NewDocument();
        }

        var document = AcadApp.DocumentManager.MdiActiveDocument
            ?? throw new InvalidOperationException("no drawing is open in AutoCAD");
        var engine = StepEngine.For(document);

        switch (method)
        {
            case "status":
                return Status(document, engine);

            case "info":
                return DrawingApi.Info(document);

            case "get_steps":
                return Steps(engine, p);

            case "reload":
                engine.Reload();
                return Status(document, engine);

            case "apply":
            {
                var record = JournalJson.RecordFromNode(p["step"] ?? p)
                    ?? throw new InvalidOperationException("the step could not be read");
                return Respond(document, engine,
                    engine.Apply(record.Op, record.Label, record.Params, record.Primitives));
            }

            case "plan":
            {
                if (p["steps"] is not JsonArray array)
                {
                    throw new InvalidOperationException("plan needs a steps array");
                }
                var steps = new List<StepRecord>();
                foreach (var node in array)
                {
                    if (JournalJson.RecordFromNode(node) is { } step)
                    {
                        steps.Add(step);
                    }
                }
                return Respond(document, engine, engine.SetPlan(Str(p, "description"), steps));
            }

            case "run_next":
                return Respond(document, engine, engine.RunNext());
            case "run_all":
                return Respond(document, engine, engine.RunAll());
            case "run_to":
                return Respond(document, engine, engine.RunTo(Int(p, "index")));
            case "rollback_to":
                return Respond(document, engine,
                    engine.RollbackTo(Int(p, "index"), Bool(p, "force")));
            case "reject":
                return Respond(document, engine, engine.Reject(Int(p, "index")));
            case "accept":
                return Respond(document, engine,
                    engine.SetAccepted(Int(p, "index"), Bool(p, "accepted", true)));
            case "note":
                return Respond(document, engine, engine.SetNote(Int(p, "index"), Str(p, "note")));
            case "snapshot":
                return Respond(document, engine, engine.Snapshot(Str(p, "note")));
            case "clear_plan":
                return Respond(document, engine, engine.ClearPlan());
            case "reset":
                return Respond(document, engine, engine.Reset());

            case "update":
            {
                var patch = p["patch"] as JsonObject
                    ?? throw new InvalidOperationException("update needs a patch object");
                return Respond(document, engine, engine.Update(Int(p, "index"), patch));
            }

            case "ensure_layer":
                return DrawingApi.EnsureLayer(
                    document, Str(p, "layer", "0"), NullableInt(p, "color"), Str(p, "linetype"),
                    NullableDouble(p, "lineweight"));

            case "blocks":
            {
                var source = NullableStr(p, "source");
                return new JsonObject
                {
                    ["source"] = source,
                    ["blocks"] = DrawingApi.Blocks(document, source, Int(p, "limit", 500)),
                };
            }

            case "ensure_block":
                return DrawingApi.EnsureBlock(
                    document, Str(p, "block", Str(p, "name")), NullableStr(p, "source"));

            case "set_units_mm":
                return DrawingApi.SetUnitsMm(document);

            case "zoom_extents":
                return DrawingApi.ZoomExtents(document);

            case "entities":
                return new JsonObject
                {
                    ["entities"] = DrawingApi.Entities(document, NullableStr(p, "layer"), Int(p, "limit", 200)),
                };

            default:
                throw new InvalidOperationException($"unknown method '{method}'");
        }
    }

    private static JsonObject Respond(Document document, StepEngine engine, StepOutcome outcome)
    {
        var touched = new JsonArray();
        foreach (var index in outcome.Touched)
        {
            touched.Add(index);
        }

        var result = new JsonObject
        {
            ["ok"] = outcome.Ok,
            ["action"] = outcome.Action,
            ["message"] = outcome.Message,
            ["touched"] = touched,
        };
        if (outcome.Refusal is { } refusal)
        {
            result["refusal"] = refusal;
        }
        if (outcome.Touched.Count > 0 && engine.Journal.At(outcome.Touched[^1]) is { } record)
        {
            result["step"] = Row(record);
        }

        // The status travels with the outcome so a client that just changed
        // something does not need a second round trip to see the result.
        return new JsonObject
        {
            ["result"] = result,
            ["status"] = Status(document, engine),
        };
    }

    private static JsonObject Status(Document document, StepEngine engine)
    {
        var journal = engine.Journal;
        var unrecoverable = new JsonArray();
        foreach (var index in journal.StepsWithoutUndo())
        {
            unrecoverable.Add(index);
        }

        return new JsonObject
        {
            ["document"] = document.Name,
            ["path"] = document.Database.Filename,
            ["description"] = journal.Description,
            ["count"] = journal.Count,
            ["applied"] = journal.Applied.Count(),
            ["planned"] = journal.Records.Count(r => r.State == StepState.Planned),
            ["failed"] = journal.Records.Count(r => r.State == StepState.Failed),
            ["accepted"] = journal.Records.Count(r => r.Accepted),
            ["drifted"] = journal.Records.Count(r => r.Drifted),
            ["unreadable"] = journal.Unreadable,
            ["unrecoverable"] = unrecoverable,
            ["queued"] = IdleQueue.Depth,
        };
    }

    private static JsonObject Steps(StepEngine engine, JsonObject p)
    {
        var offset = Math.Max(0, Int(p, "offset"));
        var limit = Math.Clamp(Int(p, "limit", 200), 1, MaxStepsPerPage);

        var steps = new JsonArray();
        foreach (var record in engine.Journal.Records.Skip(offset).Take(limit))
        {
            steps.Add(Row(record));
        }
        return new JsonObject
        {
            ["total"] = engine.Journal.Count,
            ["offset"] = offset,
            ["steps"] = steps,
        };
    }

    private static JsonObject Row(StepRecord record)
    {
        var handles = new JsonArray();
        foreach (var handle in record.ObjectsAfter)
        {
            handles.Add(handle);
        }

        return new JsonObject
        {
            ["index"] = record.Index,
            ["state"] = record.State.ToString().ToLowerInvariant(),
            ["op"] = record.Op,
            ["label"] = record.Label,
            ["note"] = record.Note,
            ["result"] = record.Result,
            ["error"] = record.Error,
            ["accepted"] = record.Accepted,
            ["drifted"] = record.Drifted,
            ["executable"] = record.Executable,
            ["mutated"] = record.Mutated,
            ["atomic"] = record.Atomic,
            ["transaction"] = record.Transaction,
            ["duration_ms"] = record.DurationMs,
            ["handles"] = handles,
            // The primitives themselves, not a count of them. The plugin is the owner of
            // this journal, so this row is all a reader on the Python side ever sees: it has
            // no local Journal to fall back on, and `summary`/`critique`/`suggest`/
            // `check_standard`/`element_capture` all read the geometry and the arguments off
            // these two keys. A count here made every one of them blind on a live drawing —
            // `drawing_critique` found nothing to complain about in a plan with a floating
            // door in it, and `drawing_finalize`, which gates on it, passed.
            ["primitives"] = JournalJson.PrimitivesForWire(record.Primitives),
            ["params"] = record.Params is null ? new JsonObject() : record.Params.DeepClone(),
            ["row"] = record.RowText(),
            ["tooltip"] = record.TooltipText(),
            ["mark"] = record.StateMark(),
        };
    }

    // ---------------------------------------------------------------- responses

    private static string Ok(JsonNode? id, JsonObject result) =>
        new JsonObject { ["id"] = id?.DeepClone(), ["ok"] = true, ["result"] = result }.ToJsonString();

    private static string Error(JsonNode? id, string message) =>
        new JsonObject { ["id"] = id?.DeepClone(), ["ok"] = false, ["error"] = message }.ToJsonString();

    private static string Blocked(JsonNode? id, string method, string reason) =>
        new JsonObject
        {
            ["id"] = id?.DeepClone(),
            ["ok"] = false,
            ["blocked"] = true,
            ["method"] = method,
            ["error"] = reason,
        }.ToJsonString();

    // ---------------------------------------------------------------- parameters

    private static string Str(JsonObject p, string key, string fallback = "") =>
        p[key] is JsonValue v && v.TryGetValue<string>(out var s) && s is not null ? s : fallback;

    private static string? NullableStr(JsonObject p, string key)
    {
        var value = Str(p, key);
        return value.Length == 0 ? null : value;
    }

    private static int Int(JsonObject p, string key, int fallback = 0) =>
        p[key] is JsonValue v && v.TryGetValue<double>(out var d) && !double.IsNaN(d)
            ? (int)d
            : fallback;

    private static int? NullableInt(JsonObject p, string key) =>
        p[key] is JsonValue v && v.TryGetValue<double>(out var d) && !double.IsNaN(d)
            ? (int)d
            : null;

    private static double? NullableDouble(JsonObject p, string key) =>
        p[key] is JsonValue v && v.TryGetValue<double>(out var d) && !double.IsNaN(d) ? d : null;

    private static bool Bool(JsonObject p, string key, bool fallback = false) =>
        p[key] is JsonValue v && v.TryGetValue<bool>(out var b) ? b : fallback;

    private static TimeSpan ReadTimeout()
    {
        var raw = Environment.GetEnvironmentVariable("ACADMCP_RPC_TIMEOUT_MS");
        return int.TryParse(raw, out var ms) && ms > 0
            ? TimeSpan.FromMilliseconds(ms)
            : TimeSpan.FromSeconds(30);
    }

    public void Dispose()
    {
        try
        {
            _stop.Cancel();
            _listener.Stop();
        }
        catch (System.Exception error)
        {
            PluginLog.Write("rpc shutdown was untidy", error);
        }
        finally
        {
            _stop.Dispose();
            if (ReferenceEquals(Current, this))
            {
                Current = null;
            }
        }
    }
}
