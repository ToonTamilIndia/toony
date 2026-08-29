"""Tool registry and the built-in tools.

Importing this package registers every built-in tool on :data:`REGISTRY`.
"""

from __future__ import annotations

from .registry import REGISTRY, Tool, ToolContext, tool

# Import for side effects: each module registers its tools on REGISTRY.
from . import applications  # noqa: F401,E402
from . import clipboard     # noqa: F401,E402
from . import code          # noqa: F401,E402
from . import desktop       # noqa: F401,E402
from . import files         # noqa: F401,E402
from . import logs          # noqa: F401,E402
from . import media         # noqa: F401,E402
from . import memory        # noqa: F401,E402
from . import network       # noqa: F401,E402
from . import packages      # noqa: F401,E402
from . import power         # noqa: F401,E402
from . import screen        # noqa: F401,E402
from . import shell         # noqa: F401,E402
from . import system        # noqa: F401,E402
from . import timers        # noqa: F401,E402
from . import web           # noqa: F401,E402

__all__ = ["REGISTRY", "Tool", "ToolContext", "tool"]
