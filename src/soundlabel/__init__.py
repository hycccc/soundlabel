"""soundlabel — an open framework for running an AI music label."""

from .brief import Brief, BlindBrief
from .catalog import Catalog

__version__ = "0.7.0"
__all__ = ["Brief", "BlindBrief", "Catalog", "__version__"]
