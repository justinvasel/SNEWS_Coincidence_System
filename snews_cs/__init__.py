from .core.logging import initialize_logging
import pandas as pd

initialize_logging("debug")
pd.options.mode.chained_assignment = None

try:
    from ._version import version as __version__
except ImportError:
    pass
