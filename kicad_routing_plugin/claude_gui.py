"""
KiCad Routing Tools - Claude Tab

Runs Claude Code headless to drive the project's AI skills from the GUI
(GitHub issue #40; groundwork for #34 and #39).

First slice: a single test button that runs the /recommend-stackup skill
on the current board and shows the result, proving the plumbing — spawn
`claude -p`, keep the wx GUI responsive on a background thread, parse the
JSON output, and surface a machine-readable value back into the plugin.
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


def find_claude():
    """Return the path to the claude CLI, or None if not installed."""
    path = shutil.which("claude")
    if path:
        return path
    for candidate in _CLAUDE_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


class ClaudeTab(wx.Panel):
    """Claude tab: run AI skills headless and bring results into the GUI."""

    def __init__(self, parent, board_filename, log_callback=None):
        super().__init__(parent)
        self.board_filename = board_filename
        self.log_callback = log_callback
        self._process = None
        self._worker = None
        self._cancel_requested = False
        self._elapsed_timer = wx.Timer(self)
        self._elapsed_seconds = 0
        self.Bind(wx.EVT_TIMER, self._on_elapsed_tick, self._elapsed_timer)
        self._claude_path = find_claude()
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
        if self._worker and self._worker.is_alive():
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
        cmd = [
            self._claude_path, "-p", prompt,
            # stream-json (requires --verbose in -p mode) emits one JSON event
            # per line as Claude works, so we can show live progress.
            "--output-format", "stream-json", "--verbose",
            "--allowedTools", "Read,Glob,Grep,Bash,WebSearch",
        ]

        self._cancel_requested = False
        self.test_btn.Disable()
        self.cancel_btn.Enable()
        self.parsed_ctrl.SetValue("")
        self.output_ctrl.SetValue(f"Running /{_TEST_SKILL} on {os.path.basename(board)} ...\n"
                                  "(local analysis; typically a few minutes)\n\n")
        self._elapsed_seconds = 0
        self.elapsed_label.SetLabel("0s")
        self._elapsed_timer.Start(1000)
        self._log(f"Claude: running /{_TEST_SKILL} on {board}")

        self._worker = threading.Thread(target=self._run_claude, args=(cmd,), daemon=True)
        self._worker.start()

    def _run_claude(self, cmd):
        """Background thread: stream claude CLI events and post them to the GUI."""
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
                    wx.CallAfter(self._on_stream_event, event)
            self._process.wait()
            stderr_thread.join(timeout=5)
            stderr = "".join(stderr_chunks)
            returncode = self._process.returncode
        except Exception as e:
            wx.CallAfter(self._on_finished, None, f"Failed to launch claude: {e}", -1)
            return
        finally:
            self._process = None
        if self._cancel_requested:
            wx.CallAfter(self._on_finished, None, "Cancelled.", returncode)
        else:
            wx.CallAfter(self._on_finished, final_event, stderr, returncode)

    # ----------------------------------------------------------- streaming

    def _on_stream_event(self, event):
        """Render one stream-json event as a live transcript line."""
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
            self.output_ctrl.AppendText("\n".join(lines) + "\n\n")
        elif etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                btype = block.get("type")
                if btype == "text" and block.get("text", "").strip():
                    self.output_ctrl.AppendText(block["text"].rstrip() + "\n")
                elif btype == "tool_use":
                    summary = self._summarize_tool_use(
                        block.get("name", "?"), block.get("input", {}))
                    self.output_ctrl.AppendText(f"  -> {summary}\n")
        elif etype == "user":
            content = event.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        text = self._tool_result_text(block)
                        mark = "x" if block.get("is_error") else "ok"
                        self.output_ctrl.AppendText(f"     [{mark}] {text}\n")

    @staticmethod
    def _summarize_tool_use(name, tool_input):
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

    @staticmethod
    def _tool_result_text(block, max_len=120):
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

    # ------------------------------------------------------------- results

    def _on_finished(self, final_event, stderr, returncode):
        self._elapsed_timer.Stop()
        self.gauge.SetValue(0)
        self.test_btn.Enable()
        self.cancel_btn.Disable()

        if final_event is None:
            # Launch failure, cancel, or claude died before emitting a result
            message = (stderr or "").strip() or f"claude exited with code {returncode}"
            self.output_ctrl.AppendText(f"\n{message}\n")
            self._log(f"Claude: {message}")
            return

        if final_event.get("is_error"):
            error = str(final_event.get("result", "unknown error from claude"))
            self.output_ctrl.AppendText(f"\nERROR: {error}\n")
            self._log(f"Claude: error: {error}")
            return

        # The streamed transcript already shows the full report; just close out.
        self.output_ctrl.AppendText(f"\n--- done in {self.elapsed_label.GetLabel()} ---\n")
        result_text = str(final_event.get("result", ""))
        parsed = self._extract_result_line(result_text)
        if parsed is not None:
            self.parsed_ctrl.SetValue(parsed)
            self._log(f"Claude: done in {self._elapsed_seconds}s, RESULT={parsed}")
        else:
            self.parsed_ctrl.SetValue("(no RESULT= line found)")
            self._log(f"Claude: done in {self._elapsed_seconds}s, no RESULT= line")

    @staticmethod
    def _extract_result_line(text):
        """Return the value of the last RESULT=<value> line, or None."""
        for line in reversed(text.strip().splitlines()):
            line = line.strip()
            if line.startswith("RESULT="):
                return line[len("RESULT="):].strip()
        return None

    # -------------------------------------------------------------- helpers

    def _on_cancel(self, event):
        self._cancel_requested = True
        proc = self._process
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
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
