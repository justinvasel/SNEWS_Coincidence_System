from snews_cs.core.logging import initialize_logging
import pandas as pd

initialize_logging("debug")
pd.options.mode.chained_assignment = None

try:
    from snews_cs._version import version
    __version__ = version

except ImportError:
    pass
