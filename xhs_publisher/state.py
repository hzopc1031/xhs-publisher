"""本地状态：记录 fill 打开的是哪个标签页，供 publish 使用。只存标签页 id/标题等，不存任何 cookie。"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional


def state_dir() -> Path:
    d = Path(os.environ.get("XHS_STATE_DIR") or (Path.cwd() / "state"))
    d.mkdir(parents=True, exist_ok=True)
    (d / "screenshots").mkdir(exist_ok=True)
    return d


def _key(profile_dir: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]+", "_", profile_dir) or "profile"


def state_path(profile_dir: str) -> Path:
    return state_dir() / f"{_key(profile_dir)}.json"


def save(profile_dir: str, data: dict) -> Path:
    p = state_path(profile_dir)
    data = dict(data, updated_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load(profile_dir: str) -> Optional[dict]:
    p = state_path(profile_dir)
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def screenshot_path(kind: str, profile_dir: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return str(state_dir() / "screenshots" / f"{ts}-{_key(profile_dir)}-{kind}.png")
