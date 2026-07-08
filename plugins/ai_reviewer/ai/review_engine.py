from typing import Any, Dict

from .llm_client import LLMClient
from .prompt_builder import build_review_prompt
from ..ui.settings import PluginSettings


def run_review(context: Dict[str, Any]) -> Dict[str, Any]:
    prompt = build_review_prompt(context)
    cfg = PluginSettings().load_runtime_config()
    client = LLMClient(
        provider=str(cfg.get("provider", "openai")),
        api_key=str(cfg.get("openai_api_key", "")),
        model=str(cfg.get("openai_model", "gpt-4.1-mini")),
        temperature=float(cfg.get("temperature", 0.2)),
    )
    llm_output = client.review(prompt, context)

    findings = []
    drc = context.get("drc", {})
    erc = context.get("erc", {})

    if drc.get("returncode") == 127:
        findings.append(
            {
                "level": "info",
                "title": "DRC not executed",
                "details": "kicad-cli is not available in PATH, so DRC was not run.",
            }
        )
    elif not drc.get("ok", False):
        drc_issues = drc.get("issue_count", 0)
        issue_text = f" ({drc_issues} issues detected)" if isinstance(drc_issues, int) and drc_issues > 0 else ""
        findings.append(
            {
                "level": "warning",
                "title": "DRC failures detected",
                "details": f"Run DRC report and resolve blocking errors before manufacturing{issue_text}.",
            }
        )

    if erc.get("returncode") == 127:
        findings.append(
            {
                "level": "info",
                "title": "ERC not executed",
                "details": "kicad-cli is not available in PATH, so ERC was not run.",
            }
        )
    elif not erc.get("ok", False):
        erc_issues = erc.get("issue_count", 0)
        issue_text = f" ({erc_issues} issues detected)" if isinstance(erc_issues, int) and erc_issues > 0 else ""
        findings.append(
            {
                "level": "warning",
                "title": "ERC failures detected",
                "details": f"Schematic ERC reports unresolved rule violations{issue_text}.",
            }
        )

    if not findings:
        findings.append(
            {
                "level": "info",
                "title": "Baseline checks completed",
                "details": "No immediate DRC/ERC blocker detected by the orchestrator.",
            }
        )

    return {
        "summary": "Initial AI review completed with local rule signals.",
        "findings": findings,
        "llm": llm_output,
    }
