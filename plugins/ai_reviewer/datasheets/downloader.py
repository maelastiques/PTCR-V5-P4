import json
import os
from typing import Any, Dict, List


def fetch_datasheets(candidates: List[Dict[str, Any]], output_dir: str) -> List[Dict[str, Any]]:
    os.makedirs(output_dir, exist_ok=True)
    # Network fetching is intentionally deferred; this creates a structured placeholder.
    results = []
    for item in candidates:
        ref = item.get("ref", "unknown") or "unknown"
        meta_path = os.path.join(output_dir, f"{ref}.json")
        payload = {
            "ref": ref,
            "status": "not_downloaded",
            "reason": "downloader not configured",
            "queries": item.get("candidate_queries", []),
        }
        with open(meta_path, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
        results.append(payload)
    return results
