"""草稿读取与校验（纯本地，不碰浏览器）。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

TITLE_MAX = 20
BODY_MAX = 1000
IMAGES_MIN, IMAGES_MAX = 1, 18
TOPICS_MIN, TOPICS_MAX = 3, 5
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


class DraftError(ValueError):
    pass


@dataclass
class Draft:
    title: str
    body: str
    images: List[str]
    topics: List[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    def topics_text_len(self) -> int:
        # 每个话题在正文里大约占 "#话题 " 的长度；用于估算 1000 字上限
        return sum(len(t) + 2 for t in self.topics)


def _norm_topic(t: str) -> str:
    t = t.strip()
    while t.startswith("#") or t.startswith("＃"):
        t = t[1:]
    if t.endswith("[话题]#"):
        t = t[: -len("[话题]#")]
    return t.strip()


def load_draft(
    input_path: Optional[str] = None,
    title: Optional[str] = None,
    body: Optional[str] = None,
    body_file: Optional[str] = None,
    images: Optional[List[str]] = None,
    topics: Optional[List[str]] = None,
) -> Draft:
    """命令行参数优先级高于 --input JSON。JSON 中的相对路径相对于 JSON 文件所在目录。"""
    data = {}
    base = Path.cwd()
    if input_path:
        p = Path(input_path).expanduser()
        if not p.is_file():
            raise DraftError(f"找不到草稿文件: {p}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise DraftError(f"草稿 JSON 解析失败: {e}") from e
        base = p.resolve().parent

    def rel(x: str, b: Path) -> str:
        q = Path(x).expanduser()
        return str(q if q.is_absolute() else (b / q).resolve())

    t = title if title is not None else data.get("title")
    if body_file:
        bf = Path(body_file).expanduser()
        if not bf.is_file():
            raise DraftError(f"找不到正文文件: {bf}")
        b = bf.read_text(encoding="utf-8")
    elif body is not None:
        b = body
    elif data.get("body_file"):
        bf = Path(rel(data["body_file"], base))
        if not bf.is_file():
            raise DraftError(f"找不到正文文件: {bf}")
        b = bf.read_text(encoding="utf-8")
    else:
        b = data.get("body")

    if images:
        imgs = [rel(i, Path.cwd()) for i in images]
    else:
        imgs = [rel(i, base) for i in data.get("images", [])]
    tps = topics if topics else data.get("topics", [])

    if t is None:
        raise DraftError("缺少标题（--title 或 JSON 的 title）")
    if b is None:
        raise DraftError("缺少正文（--body / --body-file 或 JSON 的 body / body_file）")
    d = Draft(
        title=str(t).strip(),
        body=str(b).replace("\r\n", "\n").strip("\n"),
        images=imgs,
        topics=[x for x in (_norm_topic(str(x)) for x in tps) if x],
    )
    validate(d)
    return d


def validate(d: Draft) -> None:
    errs = []
    if not d.title:
        errs.append("标题为空")
    if len(d.title) > TITLE_MAX:
        errs.append(f"标题 {len(d.title)} 字，超过 {TITLE_MAX} 字上限：{d.title!r}")
    if not d.body.strip():
        errs.append("正文为空")
    total = len(d.body) + (2 if d.topics else 0) + d.topics_text_len()
    if total > BODY_MAX:
        errs.append(f"正文+话题约 {total} 字，超过 {BODY_MAX} 字上限（正文 {len(d.body)} 字）")
    if not (IMAGES_MIN <= len(d.images) <= IMAGES_MAX):
        errs.append(f"图片数量 {len(d.images)}，需要 {IMAGES_MIN}-{IMAGES_MAX} 张")
    for i in d.images:
        p = Path(i)
        if not p.is_file():
            errs.append(f"图片不存在: {i}")
        elif p.suffix.lower() not in IMAGE_EXTS:
            errs.append(f"图片格式不支持（仅 jpg/jpeg/png/webp）: {i}")
    if not (TOPICS_MIN <= len(d.topics) <= TOPICS_MAX):
        errs.append(f"话题数量 {len(d.topics)}，需要 {TOPICS_MIN}-{TOPICS_MAX} 个")
    if len(set(d.topics)) != len(d.topics):
        errs.append("话题有重复")
    if errs:
        raise DraftError("草稿校验失败：\n  - " + "\n  - ".join(errs))
