from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class ProjectContext:
    data: Dict[str, Any]
