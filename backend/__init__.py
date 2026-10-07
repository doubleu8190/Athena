"""Source-tree package bridge for the restructured application.

The project uses a ``src`` layout.  Extending the package path keeps
``python -m backend.main`` usable from a checkout before the
package is installed editable.
"""

from __future__ import annotations

from pathlib import Path

_src_package = Path(__file__).resolve().parent / "src" 
if _src_package.is_dir():
    __path__.append(str(_src_package))  # type: ignore[name-defined]
