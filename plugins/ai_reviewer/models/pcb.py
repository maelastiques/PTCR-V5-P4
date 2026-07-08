from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class PCBContext:
    data: Dict[str, Any]
