import logging
import os
import re
import sys

from rich.console import Console
from rich.highlighter import ReprHighlighter
from rich.logging import RichHandler
from rich.style import Style
from rich.text import Span, Text

RICH_FORMAT = logging.Formatter("%(name)s %(message)s")
DEBUG_MODE = os.getenv("DEBUG_LOGGING", "1") == "1"


class _Highlighter(ReprHighlighter):
    """Apply styles to log message."""

    module_name_re = r"([^\s]+)"

    # extend default styles to include common error messages (styled same as False boolean)
    error_re = r"(?P<bool_false>\w*(ERROR|Error)\w*)"
    ReprHighlighter.highlights.append(error_re)

    def highlight(self, text: Text) -> None:
        """Apply highlighting styles."""
        super().highlight(text)
        plain = text.plain
        insert = text.spans.insert

        # style module name
        start, end = next(iter(re.finditer(self.module_name_re, plain))).span()
        insert(0, Span(start, end, Style(color="steel_blue", bold=True)))


def _create_console_logger() -> logging.Handler:
    """Return the console Logger."""

    handler = RichHandler(
        rich_tracebacks=True,
        log_time_format="[%H:%M:%S.%f]",
        keywords=[],  # disable rich keywords
        highlighter=_Highlighter(),
        console=Console(file=sys.stdout),
        show_time=DEBUG_MODE,
        show_level=DEBUG_MODE,
        show_path=DEBUG_MODE,
    )
    handler.setFormatter(RICH_FORMAT)
    handler.setLevel(logging.DEBUG)

    logging.getLogger("spotipy").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("tidalapi").setLevel(logging.INFO)
    logging.getLogger("telegram.Bot").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    return handler


def init_logging() -> None:
    logging.basicConfig(
        handlers=[_create_console_logger()],
        level=logging.DEBUG if DEBUG_MODE else logging.INFO,
    )
