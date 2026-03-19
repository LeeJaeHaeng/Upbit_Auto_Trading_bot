"""
환경 변수 로딩 유틸리티.

여러 진입점(main.py, dashboard.py)에서 동일한 .env 로딩 로직을 재사용합니다.
"""

import os
from pathlib import Path


def load_env_file(base_dir: Path | None = None, filename: str = ".env") -> Path | None:
    """
    지정 경로의 .env 파일을 읽어 환경변수를 설정합니다.

    - 이미 설정된 환경변수는 덮어쓰지 않습니다.
    - 파일이 없으면 None을 반환합니다.
    """
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent

    env_path = base_dir / filename
    if not env_path.exists():
        return None

    with env_path.open(encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key:
                continue
            os.environ.setdefault(key, value.strip())

    return env_path
