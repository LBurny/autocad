using System.Drawing;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Windows.Forms;
using AcadMcp.Core;
using AcadApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace AcadMcp.Plugin;

/// <summary>
/// The steps palette: the journal, the actions that drive it, and a log.
/// </summary>
/// <remarks>
/// <para>
/// It calls <see cref="StepEngine"/> directly, in process. That is the point of the
/// palette: the journal lives in the drawing, so every step can be read, replayed
/// and rolled back with no MCP server running and no Python involved.
/// </para>
/// <para>
/// The journal is re-read on every poll rather than trusted from memory. A
/// <c>Ctrl+Z</c> in AutoCAD changes the drawing's journal without going through
/// this panel, and a panel that showed its own remembered copy would start lying
/// to the user exactly when they were undoing something.
/// </para>
/// </remarks>
public sealed class StepsControl : UserControl
{
    private const int PollMs = 1000;

    /// <summary>Smallest useful height for each pane of the split.</summary>
    private const int MinList = 90;
    private const int MinDetail = 190;
    private const int MaxLogChars = 200_000;

    private static readonly Color Background = Color.FromArgb(30, 30, 30);
    private static readonly Color Sunken = Color.FromArgb(24, 24, 24);
    private static readonly Color Foreground = Color.FromArgb(225, 225, 225);
    private static readonly Color Muted = Color.FromArgb(150, 150, 150);
    private static readonly Color Applied = Color.FromArgb(150, 220, 150);
    private static readonly Color Planned = Color.FromArgb(150, 170, 200);
    private static readonly Color Failed = Color.FromArgb(240, 150, 140);
    private static readonly Color Drifted = Color.FromArgb(240, 200, 130);

    private readonly ListView _list;
    private readonly TextBox _detail;
    private readonly TextBox _patch;
    private readonly TextBox _log;
    private readonly Label _summary;
    private readonly ToolStripButton _runNext;
    private readonly ToolStripButton _runAll;
    private readonly ToolStripButton _rollback;
    private readonly ToolStripButton _runTo;
    private readonly ToolStripButton _accept;
    private readonly ToolStripButton _reject;
    private readonly ToolStripButton _snapshot;
    private readonly Label _hint;
    private readonly SplitContainer _split;
    private readonly System.Windows.Forms.Timer _poll;

    /// <summary>What the list was last built from, so an unchanged journal is not redrawn.</summary>
    private string _signature = "";

    /// <summary>The selected step, remembered across rebuilds.</summary>
    private int _selected;

    public StepsControl()
    {
        BackColor = Background;
        ForeColor = Foreground;
        Dock = DockStyle.Fill;

        _list = new ListView
        {
            Dock = DockStyle.Fill,
            View = View.Details,
            FullRowSelect = true,
            MultiSelect = false,
            HideSelection = false,
            BackColor = Background,
            ForeColor = Foreground,
            BorderStyle = BorderStyle.None,
            ShowItemToolTips = true,
        };
        _list.Columns.Add("#", 36);
        _list.Columns.Add("state", 76);
        _list.Columns.Add("step", 190);
        _list.Columns.Add("n", 32);
        _list.SelectedIndexChanged += (_, _) => OnSelected();
        _list.DoubleClick += (_, _) => RunToSelected();

        _detail = new TextBox
        {
            Dock = DockStyle.Fill,
            Multiline = true,
            ReadOnly = true,
            ScrollBars = ScrollBars.Both,
            BackColor = Sunken,
            ForeColor = Foreground,
            BorderStyle = BorderStyle.None,
            Font = new Font("Consolas", 9f),
            WordWrap = false,
            Text = "Select a step to see what it holds.",
        };

        _patch = new TextBox
        {
            Dock = DockStyle.Fill,
            Multiline = true,
            ScrollBars = ScrollBars.Vertical,
            BackColor = Color.FromArgb(40, 40, 42),
            ForeColor = Foreground,
            BorderStyle = BorderStyle.FixedSingle,
            Font = new Font("Consolas", 9f),
        };
        _patch.KeyDown += (_, e) =>
        {
            // Enter has to insert a newline because the patch is JSON; Ctrl+Enter
            // is what applies it.
            if (e.Control && e.KeyCode == Keys.Enter)
            {
                e.SuppressKeyPress = true;
                ApplyPatch();
            }
        };

        _log = new TextBox
        {
            Dock = DockStyle.Fill,
            Multiline = true,
            ReadOnly = true,
            ScrollBars = ScrollBars.Both,
            BackColor = Sunken,
            ForeColor = Muted,
            BorderStyle = BorderStyle.None,
            Font = new Font("Consolas", 9f),
            WordWrap = false,
        };

        _summary = new Label
        {
            Dock = DockStyle.Fill,
            ForeColor = Muted,
            TextAlign = ContentAlignment.MiddleLeft,
            Text = "starting up",
            Padding = new Padding(6, 0, 6, 0),
        };

        _hint = new Label
        {
            Dock = DockStyle.Fill,
            ForeColor = Muted,
            TextAlign = ContentAlignment.MiddleLeft,
            Text = "patch — JSON for this step's params, plus \"note\" or \"label\"; Ctrl+Enter applies",
            Padding = new Padding(4, 0, 4, 0),
        };

        _runNext = Tool("Run next", "Run the next step that still needs running", () => Run("run next", e => e.RunNext()));
        _runAll = Tool("Run all", "Run every remaining step", () => Run("run all", e => e.RunAll()));
        _rollback = Tool("Roll back", "Erase this step's successors and set them back to planned", RollbackSelected);
        _runTo = Tool("Run to", "Run every step up to and including this one", RunToSelected);
        _accept = Tool("Accept", "Mark this step reviewed, so a rollback will not pass it by accident", ToggleAccept);
        _reject = Tool("Reject", "Drop this step and everything after it, erasing what they drew", RejectFromSelected);
        _snapshot = Tool("Baseline", "Record a reviewed baseline at the end of the journal", Snapshot);

        // Two rows rather than one: eight buttons do not fit the palette's width, and
        // a single row would silently hide the destructive ones behind an overflow
        // chevron — exactly the buttons that should be visible.
        var row1 = Toolbar();
        row1.Items.AddRange([_runNext, _runAll, _rollback, _runTo]);
        var row2 = Toolbar();
        row2.Items.AddRange(
        [
            _accept, _reject,
            new ToolStripSeparator(), _snapshot,
            Tool("Refresh", "Re-read the journal from the drawing", () => Refresh(force: true)),
        ]);

        var stepTab = new TabPage("Step") { BackColor = Background, ForeColor = Foreground };
        stepTab.Controls.Add(BuildStepTab());
        var logTab = new TabPage("Log") { BackColor = Background, ForeColor = Foreground };
        logTab.Controls.Add(_log);
        var tabs = new TabControl { Dock = DockStyle.Fill };
        tabs.TabPages.Add(stepTab);
        tabs.TabPages.Add(logTab);
        tabs.SelectedIndex = 0;

        // No minimum sizes here: a SplitContainer starts out 150x100, and setting a
        // 190px minimum on it throws before it has ever been laid out. They are
        // applied in SizeSplit once there is room for them.
        _split = new SplitContainer
        {
            Dock = DockStyle.Fill,
            Orientation = Orientation.Horizontal,
            BackColor = Background,
        };
        _split.Panel1.Controls.Add(_list);
        _split.Panel2.Controls.Add(tabs);

        var root = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 1,
            RowCount = 4,
            BackColor = Background,
        };
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 26));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 26));
        root.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 22));
        root.Controls.Add(row1, 0, 0);
        root.Controls.Add(row2, 0, 1);
        root.Controls.Add(_split, 0, 2);
        root.Controls.Add(_summary, 0, 3);
        Controls.Add(root);

        _poll = new System.Windows.Forms.Timer { Interval = PollMs };
        _poll.Tick += (_, _) => Refresh(force: false);
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        SizeSplit();
        Note("AcadMcp steps palette ready");
        Refresh(force: true);
        _poll.Start();
    }

    /// <summary>
    /// Split the palette between the steps list and the selected step's detail.
    /// </summary>
    /// <remarks>
    /// Done here rather than in the constructor, and done again on every resize,
    /// because a SplitterDistance set before the container has a real height gets
    /// clamped to whatever happened to fit — which collapsed the detail pane down to
    /// a single line of text.
    /// </remarks>
    private void SizeSplit()
    {
        // Sized against the smaller of the two dimensions on purpose. The panes are
        // stacked, so the distance is a height — but WinForms validates it against
        // the container's *width* until the first layout pass has run, and its error
        // message says "Width" when it objects. Fitting inside both is always legal.
        var room = Math.Min(_split.Height, _split.Width) - _split.SplitterWidth;
        if (room <= MinList + MinDetail)
        {
            // Still too small to hold both panes. WinForms throws rather than
            // clamping when a distance or a minimum is out of range, and this is
            // reached for real while the palette is being created.
            return;
        }

        _split.Panel1MinSize = MinList;
        _split.Panel2MinSize = MinDetail;
        _split.SplitterDistance = Math.Clamp((int)(room * 0.42), MinList, room - MinDetail);
    }

    protected override void OnSizeChanged(EventArgs e)
    {
        base.OnSizeChanged(e);
        SizeSplit();
    }

    protected override void OnHandleDestroyed(EventArgs e)
    {
        _poll.Stop();
        base.OnHandleDestroyed(e);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _poll.Dispose();
        }
        base.Dispose(disposing);
    }

    // ---------------------------------------------------------------- the journal

    private static StepEngine? Engine()
    {
        var document = AcadApp.DocumentManager.MdiActiveDocument;
        return document is null ? null : StepEngine.For(document);
    }

    /// <summary>Re-read the journal, and redraw if anything about it changed.</summary>
    public void Refresh(bool force)
    {
        var engine = Engine();
        if (engine is null)
        {
            SetSummary("no drawing is open");
            if (force && _list.Items.Count > 0)
            {
                _list.Items.Clear();
                _signature = "";
            }
            return;
        }

        // The same rule the RPC queue uses: never touch the drawing mid-command.
        if (!IdleQueue.CanRunNow())
        {
            SetSummary("AutoCAD is busy — waiting for it to finish");
            return;
        }

        try
        {
            var journal = engine.Reload();
            var signature = Signature(journal);
            if (force || signature != _signature)
            {
                Rebuild(journal);
                _signature = signature;
            }
            else if (_list.SelectedItems.Count == 0 && _list.Items.Count > 0)
            {
                // Self-healing. The first rebuild happens while the control is still
                // being created, where setting the selection does not stick — and with
                // the signature unchanged no later poll would ever try again, leaving
                // the detail pane empty for good.
                Select(_selected);
            }
            UpdateActions();
        }
        catch (System.Exception error)
        {
            SetSummary($"could not read the journal: {error.Message}");
            PluginLog.Write("the panel could not read the journal", error);
        }
    }

    private void Rebuild(Journal journal)
    {
        var keep = _selected;
        _list.BeginUpdate();
        _list.Items.Clear();
        foreach (var record in journal.Records)
        {
            _list.Items.Add(new ListViewItem(
                [record.Index.ToString(), StateOf(record), record.DisplayName(), record.Handles.Count.ToString()])
            {
                Tag = record.Index,
                ForeColor = ColourOf(record),
                ToolTipText = record.TooltipText(),
            });
        }
        _list.EndUpdate();

        SetSummary(Summarise(journal));
        Select(keep);
    }

    private void Select(int index)
    {
        if (_list.Items.Count == 0)
        {
            OnSelected();
            return;
        }
        foreach (ListViewItem item in _list.Items)
        {
            if (item.Tag is int value && value == index)
            {
                item.Selected = true;
                _list.EnsureVisible(item.Index);
                OnSelected();
                return;
            }
        }
        // Nothing to carry over — a fresh journal, or the selected step was rejected
        // away. The end of the list is where the newest step is, which is also the one
        // a user is most likely to be looking at.
        var last = _list.Items[_list.Items.Count - 1];
        last.Selected = true;
        _list.EnsureVisible(last.Index);
        OnSelected();
    }

    private void OnSelected()
    {
        var record = SelectedRecord();
        if (record is null)
        {
            _detail.Text = "Select a step to see what it holds.";
            _patch.Text = "";
            UpdateActions();
            return;
        }

        _selected = record.Index;
        _detail.Text = Describe(record);
        _patch.Text = record.Params is null
            ? ""
            : record.Params.ToJsonString(new JsonSerializerOptions { WriteIndented = true });
        UpdateActions();
    }

    private StepRecord? SelectedRecord() =>
        _list.SelectedItems.Count > 0 && _list.SelectedItems[0].Tag is int index
            ? Engine()?.Journal.At(index)
            : null;

    private void UpdateActions()
    {
        var journal = Engine()?.Journal;
        var runnable = journal?.NextRunnable() is not null;
        var has = SelectedRecord() is not null;

        _runNext.Enabled = runnable;
        _runAll.Enabled = runnable;
        _rollback.Enabled = has;
        _runTo.Enabled = has;
        _accept.Enabled = has;
        _reject.Enabled = has;
        _snapshot.Enabled = journal is not null;
    }

    // ---------------------------------------------------------------- actions

    private void Run(string what, Func<StepEngine, StepOutcome> action)
    {
        var engine = Engine();
        if (engine is null)
        {
            Note("there is no drawing open");
            return;
        }
        if (!IdleQueue.CanRunNow())
        {
            Note($"{what}: AutoCAD is busy, try again in a moment");
            return;
        }

        try
        {
            var outcome = action(engine);
            Note(outcome.Ok
                ? $"{outcome.Action}: {outcome.Message}"
                : $"{outcome.Action} refused — {outcome.Message}");
            Refresh(force: true);
        }
        catch (System.Exception error)
        {
            // A panel must never throw into AutoCAD's message loop: the user would
            // get a .NET error dialog on top of their drawing.
            Note($"{what} failed: {error.Message}");
            PluginLog.Write($"the panel's {what} failed", error);
        }
    }

    private void RunToSelected()
    {
        if (SelectedRecord() is { } record)
        {
            Run("run to step", engine => engine.RunTo(record.Index));
        }
    }

    private void RollbackSelected()
    {
        if (SelectedRecord() is not { } record)
        {
            return;
        }
        if (!Confirm($"Erase everything after step {record.Index} and set those steps back to planned?"))
        {
            return;
        }
        Run("rollback", engine => engine.RollbackTo(record.Index));
    }

    private void RejectFromSelected()
    {
        if (SelectedRecord() is not { } record)
        {
            return;
        }
        if (!Confirm($"Drop step {record.Index} and everything after it from the journal, erasing "
            + "what they drew? This cannot be undone from here."))
        {
            return;
        }
        Run("reject", engine => engine.Reject(record.Index));
    }

    private void ToggleAccept()
    {
        if (SelectedRecord() is { } record)
        {
            Run("accept", engine => engine.SetAccepted(record.Index, !record.Accepted));
        }
    }

    private void Snapshot()
    {
        if (!Confirm("Record a baseline here? Everything applied so far counts as reviewed."))
        {
            return;
        }
        Run("baseline", engine => engine.Snapshot(""));
    }

    private void ApplyPatch()
    {
        if (SelectedRecord() is not { } record)
        {
            return;
        }

        JsonNode? parsed;
        try
        {
            parsed = JsonNode.Parse(_patch.Text.Trim().Length == 0 ? "{}" : _patch.Text);
        }
        catch (JsonException error)
        {
            Note($"that patch is not valid JSON: {error.Message}");
            return;
        }
        if (parsed is not JsonObject patch)
        {
            Note("a patch has to be a JSON object, for example {\"note\": \"checked\"}");
            return;
        }
        if (patch.Count == 0)
        {
            Note("the patch is empty, so there is nothing to change");
            return;
        }
        Run("update", engine => engine.Update(record.Index, patch));
    }

    private static bool Confirm(string question) =>
        MessageBox.Show(question, "AcadMcp", MessageBoxButtons.YesNo, MessageBoxIcon.Question)
        == DialogResult.Yes;

    // ---------------------------------------------------------------- rendering

    private static string StateOf(StepRecord record)
    {
        var state = record.State switch
        {
            StepState.Done => record.Accepted ? "accepted" : "applied",
            StepState.Failed => "failed",
            _ => "planned",
        };
        return record.Drifted ? state + " ~" : state;
    }

    private static Color ColourOf(StepRecord record) => record.Drifted
        ? Drifted
        : record.State switch
        {
            StepState.Done => Applied,
            StepState.Failed => Failed,
            _ => Planned,
        };

    /// <summary>
    /// A cheap fingerprint of everything the list shows. Rebuilding on every poll
    /// would reset the selection and the scroll position once a second.
    /// </summary>
    /// <remarks>
    /// The note and the params are part of it on purpose. Both are editable from outside
    /// this panel — <c>step_control(action='update')</c> writes either — and while they were
    /// left out, such an edit changed nothing the fingerprint could see, so the poll skipped
    /// the rebuild and the detail pane went on showing the params it was last rebuilt with.
    /// Lengths alone would not do either: a same-length rewrite is exactly the edit a person
    /// makes to a number.
    /// </remarks>
    private static string Signature(Journal journal)
    {
        var text = new StringBuilder(journal.Count * 24);
        foreach (var record in journal.Records)
        {
            text.Append(record.Index).Append(':')
                .Append((int)record.State).Append(record.Accepted ? 'A' : '.')
                .Append(record.Drifted ? 'D' : '.')
                .Append(record.Handles.Count).Append('/')
                .Append(record.Label).Append('/')
                .Append(record.Error).Append('/')
                .Append(record.Result).Append('/')
                .Append(record.Note).Append('/')
                .Append(record.Params?.ToJsonString() ?? "").Append(';');
        }
        return text.ToString();
    }

    private static string Summarise(Journal journal)
    {
        if (journal.Count == 0)
        {
            return "no steps recorded yet";
        }
        var text = $"{journal.Count} steps: {journal.Applied.Count()} applied, "
            + $"{journal.Records.Count(r => r.State == StepState.Planned)} planned";
        var failed = journal.Records.Count(r => r.State == StepState.Failed);
        if (failed > 0)
        {
            text += $", {failed} failed";
        }
        var accepted = journal.Records.Count(r => r.Accepted);
        if (accepted > 0)
        {
            text += $", {accepted} accepted";
        }
        var drifted = journal.Records.Count(r => r.Drifted);
        if (drifted > 0)
        {
            text += $", {drifted} changed by hand";
        }
        if (journal.Unreadable > 0)
        {
            // Said out loud rather than passed over: those steps can neither be replayed
            // nor rolled back, and their geometry has nothing owning it any more.
            text += $", {journal.Unreadable} record(s) unreadable";
        }
        return text;
    }

    private static string Describe(StepRecord record)
    {
        var text = new StringBuilder();
        text.Append('#').Append(record.Index).Append("  ").AppendLine(record.DisplayName());
        text.Append("state:       ").AppendLine(record.State.ToString().ToLowerInvariant()
            + (record.Accepted ? " (accepted)" : "")
            + (record.Drifted ? " (changed since it ran)" : ""));
        if (record.Op.Length > 0)
        {
            text.Append("op:          ").AppendLine(record.Op);
        }
        if (record.Transaction.Length > 0)
        {
            text.Append("transaction: ").AppendLine(record.Transaction);
        }
        if (record.DurationMs > 0)
        {
            text.Append("took:        ").Append(record.DurationMs).AppendLine(" ms");
        }
        if (record.Result.Length > 0)
        {
            text.Append("result:      ").AppendLine(record.Result);
        }
        if (record.Error.Length > 0)
        {
            text.Append("error:       ").AppendLine(record.Error);
        }
        if (record.Note.Length > 0)
        {
            text.Append("note:        ").AppendLine(record.Note);
        }
        if (record.Primitives.Count > 0)
        {
            text.AppendLine("primitives:");
            for (var i = 0; i < record.Primitives.Count; i++)
            {
                text.Append("  ").Append(i + 1).Append(". ").AppendLine(record.Primitives[i].Summary);
            }
        }
        if (record.Handles.Count > 0)
        {
            text.Append("entities:    ").AppendLine(string.Join(", ", record.Handles));
        }
        return text.ToString();
    }

    // ---------------------------------------------------------------- chrome

    private TableLayoutPanel BuildStepTab()
    {
        var apply = new Button
        {
            Text = "Apply patch",
            Dock = DockStyle.Fill,
            FlatStyle = FlatStyle.Flat,
            BackColor = Color.FromArgb(62, 62, 66),
            ForeColor = Foreground,
        };
        apply.Click += (_, _) => ApplyPatch();

        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 1,
            RowCount = 3,
            BackColor = Background,
        };
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 18));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 54));
        layout.Controls.Add(_hint, 0, 0);
        layout.Controls.Add(_detail, 0, 1);

        var patchRow = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 2,
            RowCount = 1,
            BackColor = Background,
        };
        patchRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 76));
        patchRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 24));
        patchRow.Controls.Add(_patch, 0, 0);
        patchRow.Controls.Add(apply, 1, 0);
        layout.Controls.Add(patchRow, 0, 2);
        return layout;
    }

    private ToolStripButton Tool(string text, string tooltip, Action clicked)
    {
        var button = new ToolStripButton(text)
        {
            ToolTipText = tooltip,
            DisplayStyle = ToolStripItemDisplayStyle.Text,
            ForeColor = Foreground,
        };
        button.Click += (_, _) => clicked();
        return button;
    }

    private static ToolStrip Toolbar() => new()
    {
        Dock = DockStyle.Fill,
        GripStyle = ToolStripGripStyle.Hidden,
        BackColor = Color.FromArgb(45, 45, 48),
        ForeColor = Foreground,
        Renderer = new ToolStripSystemRenderer(),
        CanOverflow = false,
        Padding = new Padding(2, 0, 2, 0),
    };

    private void SetSummary(string text)
    {
        if (_summary.Text != text)
        {
            _summary.Text = text;
        }
    }

    private void Note(string message)
    {
        _log.AppendText($"{DateTime.Now:HH:mm:ss}  {message}\r\n");
        if (_log.TextLength > MaxLogChars)
        {
            _log.Text = _log.Text[^MaxLogChars..];
        }
        SetSummary(message);
    }
}
