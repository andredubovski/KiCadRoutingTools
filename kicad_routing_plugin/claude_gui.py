"""
KiCad Routing Tools - Claude integration

Runs Claude Code headless to drive the project's AI skills from the GUI
(GitHub issues #40, #34, #39).

Building blocks:
- ClaudeSkillRunner: spawn `claude -p`, stream events to main-thread callbacks
- ClaudeSkillDialog: modal dialog that runs one skill with a live transcript
  and returns the machine-readable RESULT=<value> last line
- ClaudeTab: the Claude tab in the routing dialog (currently a smoke-test
  button that runs /recommend-stackup)
"""

import os
import json
import shutil
import threading
import subprocess

import wx

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(PLUGIN_DIR)

# KiCad launched from Finder/desktop doesn't inherit the shell PATH, so
# shutil.which() alone often misses `claude`. Check common install spots.
_CLAUDE_CANDIDATES = [
    os.path.expanduser("~/.claude/local/claude"),
    os.path.expanduser("~/.local/bin/claude"),
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
]

# The smoke-test skill: local-only analysis (no datasheet web lookups), so
# it finishes quickly while still exercising skill discovery and board access.
_TEST_SKILL = "recommend-stackup"

# Read-only analysis tools: the skills never need write access to the board.
DEFAULT_ALLOWED_TOOLS = "Read,Glob,Grep,Bash,WebSearch"

# Main models offered in the model dropdown: (label, --model value).
# None = let the CLI use the user's configured default.
MODEL_CHOICES = [
    ("Default", None),
    ("Fable 5", "claude-fable-5"),
    ("Opus 4.8", "claude-opus-4-8"),
    ("Sonnet 4.6", "claude-sonnet-4-6"),
    ("Haiku 4.5", "claude-haiku-4-5"),
]

# --effort levels accepted by the CLI ("Default" = don't pass the flag).
EFFORT_CHOICES = ["Default", "low", "medium", "high", "xhigh", "max"]


def find_claude():
    """Return the path to the claude CLI, or None if not installed."""
    path = shutil.which("claude")
    if path:
        return path
    for candidate in _CLAUDE_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def extract_result_line(text):
    """Return the value of the last RESULT=<value> line, or None."""
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("RESULT="):
            return line[len("RESULT="):].strip()
    return None


def summarize_tool_use(name, tool_input):
    """One-line human-readable summary of a tool call."""
    if name == "Bash":
        detail = tool_input.get("description") or tool_input.get("command", "")
    elif name in ("Read", "Write", "Edit"):
        detail = tool_input.get("file_path", "")
    elif name in ("Glob", "Grep"):
        detail = tool_input.get("pattern", "")
    elif name == "WebSearch":
        detail = tool_input.get("query", "")
    elif name == "WebFetch":
        detail = tool_input.get("url", "")
    else:
        detail = json.dumps(tool_input)
    detail = " ".join(str(detail).split())
    if len(detail) > 120:
        detail = detail[:120] + "..."
    return f"{name}: {detail}"


def tool_result_text(block, max_len=120):
    """First line of a tool result, truncated."""
    content = block.get("content", "")
    if isinstance(content, list):
        content = " ".join(
            c.get("text", "") for c in content
            if isinstance(c, dict) and c.get("type") == "text")
    first_line = str(content).strip().splitlines()[0] if str(content).strip() else "(no output)"
    if len(first_line) > max_len:
        first_line = first_line[:max_len] + "..."
    return first_line


def format_stream_event(event):
    """Format one stream-json event as transcript text, or None to skip it."""
    etype = event.get("type")
    if etype == "system" and event.get("subtype") == "init":
        model = event.get("model", "unknown")
        version = event.get("claude_code_version", "unknown")
        lines = [f"Claude Code {version} | model: {model}",
                 f"cwd: {event.get('cwd', '?')}"]
        skills = event.get("skills", [])
        if skills:
            shown = ", ".join(skills[:8]) + (", ..." if len(skills) > 8 else "")
            lines.append(f"skills discovered: {len(skills)} ({shown})")
        return "\n".join(lines) + "\n\n"
    if etype == "assistant":
        lines = []
        for block in event.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "text" and block.get("text", "").strip():
                lines.append(block["text"].rstrip())
            elif btype == "tool_use":
                summary = summarize_tool_use(block.get("name", "?"), block.get("input", {}))
                lines.append(f"  -> {summary}")
        return "\n".join(lines) + "\n" if lines else None
    if etype == "user":
        content = event.get("message", {}).get("content", [])
        lines = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    mark = "x" if block.get("is_error") else "ok"
                    lines.append(f"     [{mark}] {tool_result_text(block)}")
        return "\n".join(lines) + "\n" if lines else None
    return None


class ClaudeSkillRunner:
    """Runs `claude -p` headless on a background thread, streaming progress.

    Callbacks are invoked on the wx main thread:
      on_transcript(text)        - formatted transcript chunk to append
      on_done(result_text, error) - exactly one is non-None; cancel reports
                                    error="Cancelled."
    """

    def __init__(self, claude_path, on_transcript, on_done):
        self.claude_path = claude_path
        self.on_transcript = on_transcript
        self.on_done = on_done
        self._process = None
        self._thread = None
        self._cancel_requested = False

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def run(self, prompt, allowed_tools=DEFAULT_ALLOWED_TOOLS, model=None, effort=None):
        if self.is_running():
            raise RuntimeError("a Claude run is already in progress")
        cmd = [
            self.claude_path, "-p", prompt,
            # stream-json (requires --verbose in -p mode) emits one JSON event
            # per line as Claude works, so we can show live progress.
            "--output-format", "stream-json", "--verbose",
            "--allowedTools", allowed_tools,
        ]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        self._cancel_requested = False
        self._thread = threading.Thread(target=self._work, args=(cmd,), daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancel_requested = True
        proc = self._process
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass

    def _work(self, cmd):
        final_event = None
        try:
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            self._process = subprocess.Popen(
                cmd,
                cwd=ROOT_DIR,  # skill discovery is working-directory based
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                bufsize=1,  # line-buffered: one stream-json event per line
                **kwargs,
            )
            # Drain stderr concurrently so a chatty stderr can't fill its
            # pipe buffer and deadlock the stdout loop below.
            stderr_chunks = []
            stderr_thread = threading.Thread(
                target=lambda p: stderr_chunks.append(p.stderr.read()),
                args=(self._process,), daemon=True)
            stderr_thread.start()
            for line in self._process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if event.get("type") == "result":
                    final_event = event
                else:
                    text = format_stream_event(event)
                    if text:
                        wx.CallAfter(self.on_transcript, text)
            self._process.wait()
            stderr_thread.join(timeout=5)
            stderr = "".join(stderr_chunks)
            returncode = self._process.returncode
        except Exception as e:
            wx.CallAfter(self.on_done, None, f"Failed to launch claude: {e}")
            return
        finally:
            self._process = None
        wx.CallAfter(self._finish, final_event, stderr, returncode)

    def _finish(self, final_event, stderr, returncode):
        if self._cancel_requested:
            self.on_done(None, "Cancelled.")
        elif final_event is None:
            # claude died before emitting a result event
            self.on_done(None, (stderr or "").strip() or f"claude exited with code {returncode}")
        elif final_event.get("is_error"):
            self.on_done(None, str(final_event.get("result", "unknown error from claude")))
        else:
            self.on_done(str(final_event.get("result", "")), None)


class ClaudeSkillDialog(wx.Dialog):
    """Modal dialog that runs one Claude skill and shows the live transcript.

    After ShowModal() returns, `result_value` holds the parsed RESULT=<value>
    string (None if the run failed, was cancelled, or had no RESULT= line),
    and `result_text` holds Claude's full final reply.
    """

    def __init__(self, parent, title, prompt, claude_path=None,
                 allowed_tools=DEFAULT_ALLOWED_TOOLS, intro=None,
                 model=None, effort=None):
        super().__init__(parent, title=title, size=(720, 520),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.result_value = None
        self.result_text = None
        self._elapsed_seconds = 0
        self._done = False

        sizer = wx.BoxSizer(wx.VERTICAL)
        self.output_ctrl = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)
        self.output_ctrl.SetFont(
            wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        if intro:
            self.output_ctrl.SetValue(intro + "\n\n")
        sizer.Add(self.output_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        self.gauge = wx.Gauge(self, range=100)
        sizer.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.elapsed_label = wx.StaticText(self, label="0s")
        btn_sizer.Add(self.elapsed_label, 1, wx.ALIGN_CENTER_VERTICAL)
        self.action_btn = wx.Button(self, label="Cancel")
        self.action_btn.Bind(wx.EVT_BUTTON, self._on_action)
        btn_sizer.Add(self.action_btn, 0)
        sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

        self.Bind(wx.EVT_CLOSE, self._on_close)
        self._elapsed_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_elapsed_tick, self._elapsed_timer)
        self._elapsed_timer.Start(1000)

        self._runner = ClaudeSkillRunner(
            claude_path or find_claude(), self._append, self._on_done)
        self._runner.run(prompt, allowed_tools=allowed_tools, model=model, effort=effort)

    def _append(self, text):
        self.output_ctrl.AppendText(text)

    def _on_done(self, result_text, error):
        self._done = True
        self._elapsed_timer.Stop()
        self.gauge.SetValue(0)
        self.action_btn.SetLabel("Close")
        if error:
            self.output_ctrl.AppendText(f"\n{error}\n")
            return
        self.result_text = result_text
        self.result_value = extract_result_line(result_text)
        self.output_ctrl.AppendText(
            f"\n--- done in {self.elapsed_label.GetLabel()}"
            + (f" | RESULT={self.result_value}" if self.result_value is not None
               else " | no RESULT= line found")
            + " ---\n")

    def _on_action(self, event):
        if self._done:
            self.EndModal(wx.ID_OK)
        else:
            self._runner.cancel()
            self.action_btn.Disable()

    def _on_close(self, event):
        if not self._done:
            self._runner.cancel()
        self.EndModal(wx.ID_CANCEL)

    def _on_elapsed_tick(self, event):
        self._elapsed_seconds += 1
        mins, secs = divmod(self._elapsed_seconds, 60)
        self.elapsed_label.SetLabel(f"{mins}m {secs:02d}s" if mins else f"{secs}s")
        self.gauge.Pulse()


class ClaudeTab(wx.Panel):
    """Claude tab: run AI skills headless and bring results into the GUI."""

    def __init__(self, parent, board_filename, log_callback=None):
        super().__init__(parent)
        self.board_filename = board_filename
        self.log_callback = log_callback
        self._elapsed_timer = wx.Timer(self)
        self._elapsed_seconds = 0
        self.Bind(wx.EVT_TIMER, self._on_elapsed_tick, self._elapsed_timer)
        self._claude_path = find_claude()
        self._runner = None
        if self._claude_path:
            self._runner = ClaudeSkillRunner(
                self._claude_path, self._append_transcript, self._on_done)
        self._create_ui()

    def _create_ui(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Availability status
        if self._claude_path:
            status = f"Claude Code CLI found: {self._claude_path}"
        else:
            status = ("Claude Code CLI not found. Install it (https://claude.com/claude-code) "
                      "and make sure `claude` is on your PATH, then reopen this dialog.")
        self.status_label = wx.StaticText(self, label=status)
        self.status_label.Wrap(720)
        sizer.Add(self.status_label, 0, wx.ALL, 8)

        # Model / effort selection row
        sel_sizer = wx.BoxSizer(wx.HORIZONTAL)
        sel_sizer.Add(wx.StaticText(self, label="Model:"), 0,
                      wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.model_choice = wx.Choice(self, choices=[label for label, _ in MODEL_CHOICES])
        self.model_choice.SetSelection(0)
        self.model_choice.SetToolTip(
            "Model for the headless run (--model). Default = your claude CLI default. "
            "Bigger models give deeper analysis; Haiku is fastest/cheapest.")
        sel_sizer.Add(self.model_choice, 0, wx.RIGHT, 15)

        sel_sizer.Add(wx.StaticText(self, label="Effort:"), 0,
                      wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.effort_choice = wx.Choice(self, choices=EFFORT_CHOICES)
        self.effort_choice.SetSelection(0)
        self.effort_choice.SetToolTip(
            "Reasoning effort (--effort): low/medium/high/xhigh/max. Higher = more "
            "thorough but slower and costlier. Not supported on Haiku.")
        sel_sizer.Add(self.effort_choice, 0)
        sizer.Add(sel_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # Test button row
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.test_btn = wx.Button(self, label="Test: Recommend Stackup")
        self.test_btn.SetToolTip(
            f"Run the /{_TEST_SKILL} skill headless on the current board and show "
            "the result here. Analysis only - nothing is modified.")
        self.test_btn.Bind(wx.EVT_BUTTON, self._on_run_test)
        self.test_btn.Enable(self._claude_path is not None)
        btn_sizer.Add(self.test_btn, 0, wx.RIGHT, 5)

        self.cancel_btn = wx.Button(self, label="Cancel")
        self.cancel_btn.Bind(wx.EVT_BUTTON, self._on_cancel)
        self.cancel_btn.Disable()
        btn_sizer.Add(self.cancel_btn, 0, wx.RIGHT, 10)

        self.elapsed_label = wx.StaticText(self, label="")
        btn_sizer.Add(self.elapsed_label, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(btn_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # Activity gauge (pulses while Claude runs)
        self.gauge = wx.Gauge(self, range=100)
        sizer.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        # Parsed machine-readable result (the proof that values can flow
        # back into GUI fields, per issues #34/#39/#40)
        parsed_sizer = wx.BoxSizer(wx.HORIZONTAL)
        parsed_sizer.Add(wx.StaticText(self, label="Parsed result:"), 0,
                         wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.parsed_ctrl = wx.TextCtrl(self, style=wx.TE_READONLY)
        self.parsed_ctrl.SetToolTip(
            "The machine-readable last line of Claude's reply (RESULT=...), "
            "demonstrating how skill output will populate GUI fields.")
        parsed_sizer.Add(self.parsed_ctrl, 1, wx.EXPAND)
        sizer.Add(parsed_sizer, 0, wx.EXPAND | wx.ALL, 8)

        # Full output
        self.output_ctrl = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)
        self.output_ctrl.SetFont(
            wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        sizer.Add(self.output_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.SetSizer(sizer)

    # ------------------------------------------------------------------ run

    def _on_run_test(self, event):
        if self._runner is None or self._runner.is_running():
            return
        board = self.board_filename
        if not board or not os.path.isfile(board):
            wx.MessageBox(
                "Board file not found on disk. Save the board first so the "
                f"analysis sees the current state.\n\nLooked for: {board}",
                "Claude", wx.OK | wx.ICON_WARNING)
            return

        prompt = (
            f"/{_TEST_SKILL} {os.path.abspath(board)} — analysis only, do not modify "
            "any files. After the report, end your reply with exactly one line of "
            "the form RESULT=<copper layer count you recommend> (a bare integer), "
            "e.g. RESULT=4"
        )

        self.test_btn.Disable()
        self.cancel_btn.Enable()
        self.parsed_ctrl.SetValue("")
        self.output_ctrl.SetValue(f"Running /{_TEST_SKILL} on {os.path.basename(board)} ...\n"
                                  "(local analysis; typically a few minutes)\n\n")
        self._elapsed_seconds = 0
        self.elapsed_label.SetLabel("0s")
        self._elapsed_timer.Start(1000)
        model = MODEL_CHOICES[self.model_choice.GetSelection()][1]
        effort_label = EFFORT_CHOICES[self.effort_choice.GetSelection()]
        effort = None if effort_label == "Default" else effort_label
        self._log(f"Claude: running /{_TEST_SKILL} on {board}"
                  + (f" | model={model}" if model else "")
                  + (f" | effort={effort}" if effort else ""))
        self._runner.run(prompt, model=model, effort=effort)

    def _append_transcript(self, text):
        self.output_ctrl.AppendText(text)

    def _on_done(self, result_text, error):
        self._elapsed_timer.Stop()
        self.gauge.SetValue(0)
        self.test_btn.Enable()
        self.cancel_btn.Disable()

        if error:
            self.output_ctrl.AppendText(f"\n{error}\n")
            self._log(f"Claude: {error}")
            return

        # The streamed transcript already shows the full report; just close out.
        self.output_ctrl.AppendText(f"\n--- done in {self.elapsed_label.GetLabel()} ---\n")
        parsed = extract_result_line(result_text)
        if parsed is not None:
            self.parsed_ctrl.SetValue(parsed)
            self._log(f"Claude: done in {self._elapsed_seconds}s, RESULT={parsed}")
        else:
            self.parsed_ctrl.SetValue("(no RESULT= line found)")
            self._log(f"Claude: done in {self._elapsed_seconds}s, no RESULT= line")

    # -------------------------------------------------------------- helpers

    def _on_cancel(self, event):
        if self._runner is not None:
            self._runner.cancel()
        self.cancel_btn.Disable()
        self._log("Claude: cancel requested")

    def _on_elapsed_tick(self, event):
        self._elapsed_seconds += 1
        mins, secs = divmod(self._elapsed_seconds, 60)
        self.elapsed_label.SetLabel(f"{mins}m {secs:02d}s" if mins else f"{secs}s")
        self.gauge.Pulse()

    def _log(self, message):
        if self.log_callback:
            self.log_callback(message)
