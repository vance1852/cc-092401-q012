"""人形机器人结构化试验数据的基础组件。"""

from .contracts import Observation, Protocol, ValidationError
from .analysis import ALGORITHM_VERSION, analyze, bootstrap_mean_interval
from .comparison import COMPARISON_ALGORITHM_VERSION, compare
from .numeric import NumericSummary, WilsonInterval
from .service import TrialService

__all__ = [
    "NumericSummary",
    "Observation",
    "Protocol",
    "ValidationError",
    "WilsonInterval",
    "ALGORITHM_VERSION",
    "COMPARISON_ALGORITHM_VERSION",
    "TrialService",
    "analyze",
    "bootstrap_mean_interval",
    "compare",
]

__version__ = "0.1.0"
