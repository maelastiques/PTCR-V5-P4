import json
import os
from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class PluginSettings:
    context_dir_name: str = "ai_review_context"
    config_dir_name: str = ".ai_reviewer"
    config_file_name: str = "config.json"

    def context_dir(self, project_path: str) -> str:
        return os.path.join(project_path, self.context_dir_name)

    def user_config_dir(self) -> str:
        return os.path.join(os.path.expanduser("~"), self.config_dir_name)

    def user_config_path(self) -> str:
        return os.path.join(self.user_config_dir(), self.config_file_name)

    def default_runtime_config(self) -> Dict[str, Any]:
        return {
            "provider": "openai",
            "openai_model": "gpt-4.1-mini",
            "openai_api_key": "",
            "temperature": 0.2,
        }

    def load_runtime_config(self) -> Dict[str, Any]:
        config = self.default_runtime_config()
        path = self.user_config_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as stream:
                    loaded = json.load(stream)
                if isinstance(loaded, dict):
                    config.update(loaded)
            except Exception:
                pass

        env_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if env_key:
            config["openai_api_key"] = env_key

        return config

    def save_runtime_config(self, config: Dict[str, Any]) -> None:
        os.makedirs(self.user_config_dir(), exist_ok=True)
        merged = self.default_runtime_config()
        merged.update(config)
        with open(self.user_config_path(), "w", encoding="utf-8") as stream:
            json.dump(merged, stream, indent=2, ensure_ascii=False)
