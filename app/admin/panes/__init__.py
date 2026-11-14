"""The console's tabs, in display order."""

from .agents import AgentsPane
from .database import DatabasePane
from .diagnostics import DiagnosticsPane
from .docs import DocsPane
from .files import FilesPane
from .service import ServicePane

PANES = [AgentsPane, FilesPane, DatabasePane, DiagnosticsPane, ServicePane, DocsPane]
