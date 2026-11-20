from pathlib import Path

from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path


class PluginConfig:
    """Provide validated runtime settings and plugin-owned paths."""

    def __init__(self, config: AstrBotConfig):
        """Read and normalize plugin configuration.

        Args:
            config: AstrBot configuration generated from `_conf_schema.json`.
        """
        self._plugin_name = "astrbot_plugin_jiakao"

        self.question_timeout_seconds = config["question_timeout_seconds"]
        self.recent_question_count = config["recent_question_count"]
        self.data_dir = Path(get_astrbot_plugin_data_path()) / self._plugin_name
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.question_bank_path = self.data_dir / "bank.json"
        self.image_dir = self.data_dir / "images"
        self.stats_path = self.data_dir / "stats.json"
