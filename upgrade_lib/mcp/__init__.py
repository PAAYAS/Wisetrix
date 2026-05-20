"""upgrade_lib.mcp — MCP learning server for the upgrade agent."""

from upgrade_lib.mcp.lesson_store import LessonStore
from upgrade_lib.mcp.schema import Lesson, LessonQuery

# get_mcp_router is intentionally NOT imported here at package level.
# Importing it would pull in FastAPI at module load time, which triggers
# pydantic schema generation and hangs on Python 3.14.
# Import it directly when needed: from upgrade_lib.mcp.server import get_mcp_router

__all__ = ["LessonStore", "Lesson", "LessonQuery"]
