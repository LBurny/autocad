using System.Collections.Concurrent;
using AcadApp = Autodesk.AutoCAD.ApplicationServices.Core.Application;

namespace AcadMcp.Plugin;

/// <summary>Raised when AutoCAD was not in a state where the work could run.</summary>
public sealed class IdleQueueBlockedException : System.Exception
{
    public IdleQueueBlockedException(string message) : base(message) { }
}

/// <summary>
/// Runs work on AutoCAD's main thread from any other thread.
/// </summary>
/// <remarks>
/// <para>
/// The AutoCAD database is not thread-safe, so every call that touches it has to
/// run on the UI thread. Work is queued and drained from <c>Application.Idle</c>,
/// which is the sanctioned place for programmatic edits.
/// </para>
/// <para>
/// Idle is also what makes the interaction protection work, and it needs care in
/// both directions. Idle <em>does</em> fire while a command is waiting for input,
/// so draining then would slip our edits inside somebody else's command; the
/// command name is therefore checked and the queue simply waits its turn. And
/// Idle does <em>not</em> fire while a modal dialog is up, so a request made then
/// cannot run at all — rather than hang, the caller waits a bounded time and is
/// told what was in the way. That replaces the COM backend's
/// <c>RPC_E_CALL_REJECTED</c> retry loop with something that can explain itself.
/// </para>
/// <para>
/// A caller that gives up marks its work abandoned, so a request that timed out
/// can never execute later and change the drawing behind the client's back.
/// </para>
/// </remarks>
public static class IdleQueue
{
    private static readonly ConcurrentQueue<Action> Pending = new();

    private static int _uiThreadId;
    private static bool _attached;

    // Written on the main thread, read from any thread. Volatile because the
    // reader is usually a socket thread, and this is the whole reason the queue
    // can describe a stall without touching AutoCAD's API off-thread.
    private static long _lastDrainMs;
    private static string _command = "";

    public static bool Attached => _attached;

    /// <summary>How many calls are waiting to run.</summary>
    public static int Depth => Pending.Count;

    /// <summary>
    /// Start pumping. Must be called from AutoCAD's main thread — it records
    /// which thread that is, so a caller on that thread can be told to back off
    /// instead of deadlocking.
    /// </summary>
    public static void Attach()
    {
        if (_attached)
        {
            return;
        }
        _uiThreadId = Environment.CurrentManagedThreadId;
        AcadApp.Idle += OnIdle;
        _attached = true;
    }

    public static void Detach()
    {
        if (!_attached)
        {
            return;
        }
        AcadApp.Idle -= OnIdle;
        _attached = false;
        while (Pending.TryDequeue(out _))
        {
            // Dropped deliberately: these are waits nobody is waiting for.
        }
    }

    /// <summary>Queue <paramref name="work"/> and wait for it, up to a timeout.</summary>
    public static async Task<T> RunAsync<T>(
        Func<T> work,
        TimeSpan timeout,
        CancellationToken cancel = default)
    {
        if (!_attached)
        {
            throw new InvalidOperationException("the idle queue is not attached");
        }
        if (Environment.CurrentManagedThreadId == _uiThreadId)
        {
            // Idle cannot fire while this thread is blocked in the wait below, so
            // this would sit here until the timeout and then lie about being
            // blocked. Fail immediately and say why.
            throw new InvalidOperationException(
                "cannot wait for the idle queue from AutoCAD's own thread");
        }

        var completion = new TaskCompletionSource<T>(TaskCreationOptions.RunContinuationsAsynchronously);
        var abandoned = 0;
        Pending.Enqueue(() =>
        {
            if (Volatile.Read(ref abandoned) == 1)
            {
                return;
            }
            try
            {
                completion.TrySetResult(work());
            }
            catch (System.Exception error)
            {
                completion.TrySetException(error);
            }
        });

        try
        {
            return await completion.Task.WaitAsync(timeout, cancel).ConfigureAwait(false);
        }
        catch (TimeoutException)
        {
            Volatile.Write(ref abandoned, 1);
            throw new IdleQueueBlockedException(DescribeStall());
        }
    }

    /// <summary>Queue work and wait for it to finish.</summary>
    public static async Task RunAsync(Action work, TimeSpan timeout, CancellationToken cancel = default) =>
        await RunAsync(
            () =>
            {
                work();
                return true;
            },
            timeout,
            cancel).ConfigureAwait(false);

    /// <summary>
    /// Why the queue has not run, in words a user can act on. Reads only fields
    /// this class owns, so it is safe to call from any thread — which matters,
    /// because it is called precisely when AutoCAD is unreachable.
    /// </summary>
    public static string DescribeStall()
    {
        var command = Volatile.Read(ref _command);
        if (command.Length > 0)
        {
            return $"AutoCAD is in the {command} command and cannot run this yet";
        }

        var last = Volatile.Read(ref _lastDrainMs);
        if (last == 0)
        {
            return "AutoCAD has not been idle since the plugin loaded; "
                + "a modal dialog or startup prompt is probably open";
        }

        var stalled = (Environment.TickCount64 - last) / 1000.0;
        return $"AutoCAD has not been idle for {stalled:0.#}s; "
            + "a modal dialog, a prompt or a long command is holding it";
    }

    /// <summary>
    /// Whether AutoCAD will accept edits right now, and the reason recorded for
    /// later if it will not.
    /// </summary>
    /// <remarks>
    /// Main thread only: it reads <c>Document.CommandInProgress</c>. The panel and
    /// the queue share this one rule so they cannot disagree about when it is safe
    /// to touch the drawing.
    /// </remarks>
    public static bool CanRunNow()
    {
        var command = AcadApp.DocumentManager.MdiActiveDocument?.CommandInProgress ?? "";
        Volatile.Write(ref _command, command);
        return command.Length == 0;
    }

    private static void OnIdle(object? sender, EventArgs e)
    {
        // Idle also fires while a command waits for input, so running our edits
        // then would put them inside somebody else's command.
        if (!CanRunNow())
        {
            return;
        }

        Volatile.Write(ref _lastDrainMs, Environment.TickCount64);
        while (Pending.TryDequeue(out var work))
        {
            work();
        }
    }
}
