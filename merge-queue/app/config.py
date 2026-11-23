"""运行期配置：全部可通过环境变量覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Settings:
    data_dir: str = _env("MQ_DATA_DIR", "/data")
    worker_interval: float = float(_env("MQ_WORKER_INTERVAL", "1.0"))
    test_timeout: int = int(_env("MQ_TEST_TIMEOUT", "120"))
    merge_kind: str = _env("MQ_MERGE_KIND", "merge")  # merge | ff
    sample_repo_name: str = _env("MQ_SAMPLE_REPO_NAME", "sample-repo")

    @property
    def central_repo_dir(self) -> str:
        return os.path.join(self.data_dir, "central.git")

    @property
    def worktrees_dir(self) -> str:
        return os.path.join(self.data_dir, "worktrees")

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "queue.db")


settings = Settings()
