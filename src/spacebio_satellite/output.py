"""What the program prints. Human lines by default; ``--json`` turns every result and event into one JSON line on stdout."""
from __future__ import annotations

import json
import sys
from datetime import datetime


class Output:
    def __init__(self, *, json_mode=False, quiet=False, verbose=0, stdout=None, stderr=None):
        self.json_mode, self.quiet, self.verbose = json_mode, quiet, verbose
        self._stdout, self._stderr = stdout, stderr

    @property
    def stdout(self):                       # looked up at print time, so redirecting sys.stdout later (tests, shells) still works
        return self._stdout or sys.stdout

    @property
    def stderr(self):
        return self._stderr or sys.stderr

    # ---------------------------------------------------------------------------------------------- human text
    def say(self, text=""):
        if not self.json_mode and not self.quiet:
            print(text, file=self.stdout, flush=True)

    def stamp(self, text):
        """A progress line with the time, for the long-running ``run`` command."""
        self.say(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")

    def warn(self, text):
        print(f"warning: {text}", file=self.stderr, flush=True)           # --quiet hides the chatter, never the problems

    def error(self, text, hint=""):
        print(f"error: {text}", file=self.stderr, flush=True)
        if hint:
            print(f"       {hint}", file=self.stderr, flush=True)

    def debug(self, text):
        if self.verbose:
            print(f"debug: {text}", file=self.stderr, flush=True)

    # ---------------------------------------------------------------------------------------------- machine text
    def event(self, name, **data):
        """One line of JSON per event (only in --json mode)."""
        if self.json_mode:
            print(json.dumps({"event": name, **data}, default=str, ensure_ascii=False), file=self.stdout, flush=True)

    def result(self, data, lines=()):
        """The answer of a command: a JSON object in --json mode, else the given human lines."""
        if self.json_mode:
            print(json.dumps(data, default=str, ensure_ascii=False, indent=None), file=self.stdout, flush=True)
        else:
            for line in lines:
                self.say(line)
