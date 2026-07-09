from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class SchematicContext:
    data: Dict[str, Any]
