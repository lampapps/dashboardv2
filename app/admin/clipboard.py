"""Put text on the system clipboard through a desktop clipboard program, for terminals without OSC 52 support."""

import os
import shutil
import subprocess
from typing import Optional

TIMEOUT_S = 3

# (program, environment variable that shows a desktop session of that kind, command that reads the text on stdin)
TOOLS = (
    ("wl-copy", "WAYLAND_DISPLAY", ["wl-copy"]),
    ("xclip", "DISPLAY", ["xclip", "-selection", "clipboard"]),
    ("xsel", "DISPLAY", ["xsel", "--clipboard", "--input"]),
)


def copy_with_tool(text: str) -> Optional[str]:
    """The name of the clipboard program that took `text`, or None when no desktop session or program is available.

    The text goes in on stdin, never as an argument, so a secret never shows in a process listing.
    """
    for name, session_var, command in TOOLS:
        if not os.environ.get(session_var) or not shutil.which(command[0]):
            continue
        try:
            subprocess.run(command, input=text, text=True, check=True, timeout=TIMEOUT_S,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        return name
    return None
