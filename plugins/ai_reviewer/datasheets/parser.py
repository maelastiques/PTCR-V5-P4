from typing import Any, Dict, List


def parse_datasheet_corpus(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "count": len(entries),
        "entries": entries,
    }
