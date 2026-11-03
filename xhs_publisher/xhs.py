"""小红书创作服务平台页面操作。所有页面选择器集中在这里，页面改版时只改这个文件。"""
from __future__ import annotations

import re
import time
from typing import List, Optional

from playwright.sync_api import Page, TimeoutError as PWTimeout

from .draft import Draft

CREATOR_HOST = "creator.xiaohongshu.com"
PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"
NOTE_MANAGER_URL = "https://creator.xiaohongshu.com/new/note-manager"

# ---- 选择器（易变，见 README「已知限制」）
SEL_NICKNAME = ".user-info .name-box, span.name-box"
SEL_CREATOR_TAB = ".creator-tab"
SEL_IMAGE_INPUT = 'input[type="file"][accept*="png"], input[type="file"][accept*="jpg"]'
SEL_TITLE = 'input[placeholder*="标题"]'
SEL_EDITOR = 'div.tiptap.ProseMirror[contenteditable="true"]'
SEL_TOPIC_DROPDOWN = "#creator-editor-topic-container"
SEL_TOPIC_ITEM = "#creator-editor-topic-container div.item"
SEL_TOPIC_CHIP = 'a.tiptap-topic'
SEL_PUBLISH_HOST = "xhs-publish-btn"      # 自定义元素，按钮在 closed shadow root 里
SEL_NOTE_CARD = ".note-card"

# 出现这些文字/URL 视为被拦截：验证码、登录页、风控
BLOCK_TEXTS = ["登录超限", "扫码登录", "短信登录", "请完成安全验证", "安全验证", "拖动滑块", "滑块验证",
               "请输入验证码", "账号存在风险", "账号异常", "操作频繁", "访问频繁", "环境异常", "风险提示"]
BLOCK_URL_PARTS = ["/login", "captcha", "verify", "website-login"]
KNOWN_STATUSES = ["审核中", "未通过", "已发布", "仅自己可见", "已删除", "违规", "发布中", "定时发布"]


class XhsError(RuntimeError):
    pass


class Blocked(XhsError):
    pass


def log(msg: str) -> None:
    print(f"[xhs] {msg}", flush=True)


# ---------------------------------------------------------------- 通用检查
def check_blockers(page: Page) -> None:
    url = page.url
    if CREATOR_HOST not in url and "xiaohongshu.com" not in url:
        raise Blocked(f"页面不在小红书域名下: {url}")
    low = url.lower()
    for part in BLOCK_URL_PARTS:
        if part in low:
            raise Blocked(f"疑似登录/验证页面（URL 含 {part!r}）: {url}")
    try:
        text = page.evaluate(
            """() => {
                // 只看可见的弹层/对话框 + body 顶层可见文本，避免误伤正文里的字
                const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                const parts = [];
                for (const e of document.querySelectorAll('[role=dialog], .d-modal, .el-dialog, .modal, [class*=captcha], [class*=verify], [class*=login]')) {
                    if (vis(e)) parts.push(e.innerText || '');
                }
                const ed = document.querySelector('div.tiptap.ProseMirror');
                const t = document.querySelector('input[placeholder*="标题"]');
                let body = document.body ? document.body.innerText : '';
                if (ed && ed.innerText) body = body.split(ed.innerText).join('');
                return parts.join('\\n') + '\\n' + body.slice(0, 20000);
            }"""
        )
    except Exception:
        text = ""
    for kw in BLOCK_TEXTS:
        if kw in text:
            raise Blocked(f"页面出现拦截提示「{kw}」（验证码/登录/风控），已中止。请人工处理后重试。")
    # captcha iframe
    for f in page.frames:
        if any(k in f.url.lower() for k in ("captcha", "verify")):
            raise Blocked(f"页面出现验证码 iframe: {f.url}")


def read_nickname(page: Page, timeout_ms: int = 15000) -> str:
    loc = page.locator(SEL_NICKNAME).first
    try:
        loc.wait_for(state="visible", timeout=timeout_ms)
    except PWTimeout:
        check_blockers(page)
        raise XhsError("页面顶部找不到昵称（可能未登录或页面改版）")
    return (loc.inner_text() or "").strip()


def verify_nickname(page: Page, expect: str) -> str:
    nick = read_nickname(page)
    if nick != expect:
        raise XhsError(f"账号不符：页面昵称是「{nick}」，期望「{expect}」。已中止，未填写任何内容。")
    return nick


def wait_page_ready(page: Page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except PWTimeout:
        pass
    page.wait_for_timeout(1500)
    check_blockers(page)


# ---------------------------------------------------------------- 填写
def goto_publish_image(page: Page) -> None:
    page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=45000)
    wait_page_ready(page)
    page.locator(SEL_CREATOR_TAB).first.wait_for(state="attached", timeout=20000)


def click_image_tab(page: Page) -> None:
    """页面里有多个「上传图文」节点，其中一个被挪到屏幕外（-9999px），只能点视口内那个。"""
    for _ in range(3):
        ok = page.evaluate(
            """() => {
                const t = [...document.querySelectorAll('.creator-tab')].find(e => {
                    const r = e.getBoundingClientRect();
                    return (e.innerText || '').trim() === '上传图文' && r.width > 0 && r.x >= 0 && r.y >= 0
                           && r.x < innerWidth && r.y < innerHeight;
                });
                if (t) { t.click(); return true; } return false;
            }"""
        )
        if ok:
            break
        page.wait_for_timeout(1000)
    else:
        raise XhsError("找不到可点击的「上传图文」标签（页面改版？）")
    try:
        page.locator(SEL_IMAGE_INPUT).first.wait_for(state="attached", timeout=15000)
    except PWTimeout:
        raise XhsError("切换到「上传图文」后找不到图片上传 input")


def uploaded_count(page: Page) -> Optional[int]:
    """读取「图片编辑 N/18」里的 N。"""
    try:
        txt = page.evaluate("() => document.body.innerText")
    except Exception:
        return None
    m = re.search(r"图片编辑\s*(\d+)\s*/\s*18", txt)
    return int(m.group(1)) if m else None


def upload_images(page: Page, images: List[str]) -> None:
    page.locator(SEL_IMAGE_INPUT).first.set_input_files(images)
    title = page.locator(SEL_TITLE).first
    title.wait_for(state="visible", timeout=60000)
    end = time.time() + 120
    while time.time() < end:
        n = uploaded_count(page)
        if n == len(images):
            break
        page.wait_for_timeout(1000)
    else:
        raise XhsError(f"图片上传未完成：页面显示 {uploaded_count(page)} 张，期望 {len(images)} 张")
    # 等上传进度/loading 消失
    for _ in range(60):
        busy = page.evaluate(
            """() => [...document.querySelectorAll('[class*=progress],[class*=uploading],[class*=loading]')]
                     .some(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 &&
                                  e.closest('[class*=img],[class*=image],[class*=upload]'); })"""
        )
        if not busy:
            break
        page.wait_for_timeout(1000)
    check_blockers(page)


def fill_title(page: Page, title: str) -> None:
    loc = page.locator(SEL_TITLE).first
    loc.fill(title)
    page.wait_for_timeout(300)
    got = loc.input_value()
    if got != title:
        raise XhsError(f"标题写入后不一致：页面是 {got!r}，期望 {title!r}")


def _clear_editor(page: Page) -> None:
    ed = page.locator(SEL_EDITOR).first
    ed.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.wait_for_timeout(200)


def fill_body(page: Page, body: str) -> None:
    """正文用 insertText（不是逐键输入），中文不会乱码；换行用 Enter 生成新段落。"""
    ed = page.locator(SEL_EDITOR).first
    ed.wait_for(state="visible", timeout=15000)
    _clear_editor(page)
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if line:
            page.keyboard.insert_text(line)
        if i < len(lines) - 1:
            page.keyboard.press("Enter")
    page.wait_for_timeout(300)


def _move_caret_to_end(page: Page) -> None:
    page.locator(SEL_EDITOR).first.click()
    page.keyboard.press("Control+End")


def add_topics(page: Page, topics: List[str], fallback: str = "fail") -> List[str]:
    """在正文末尾空一行，逐个输入 '#话题'，从下拉中点选**完全一致**的项，变成真正的话题 chip。
    fallback: fail=没有完全匹配就报错；first=选下拉第一项；"""
    _move_caret_to_end(page)
    page.keyboard.press("Enter")
    chosen = []
    for t in topics:
        page.keyboard.insert_text("#" + t)
        dd = page.locator(SEL_TOPIC_DROPDOWN)
        try:
            dd.wait_for(state="visible", timeout=8000)
        except PWTimeout:
            raise XhsError(f"输入 #{t} 后没有出现话题下拉框")
        # 下拉是异步搜索结果，等到出现完全匹配项或超时
        exact = page.locator(SEL_TOPIC_ITEM).filter(
            has=page.locator("span.name").get_by_text("#" + t, exact=True))
        found = False
        for _ in range(16):
            if exact.count() > 0:
                found = True
                break
            page.wait_for_timeout(500)
        if found:
            exact.first.click()
            chosen.append(t)
        elif fallback == "first" and page.locator(SEL_TOPIC_ITEM).count() > 0:
            first = page.locator(SEL_TOPIC_ITEM).first
            name = (first.locator("span.name").inner_text() or "").strip().lstrip("#")
            first.click()
            chosen.append(name)
            log(f"话题 #{t} 没有完全匹配，按 --topic-fallback=first 选了 #{name}")
        else:
            opts = [x.strip() for x in page.locator(SEL_TOPIC_ITEM + " span.name").all_inner_texts()[:8]]
            raise XhsError(f"话题下拉里没有与 #{t} 完全一致的项。候选: {opts}")
        page.wait_for_timeout(600)
    return chosen


def editor_state(page: Page) -> dict:
    return page.evaluate(
        """() => {
            const ed = document.querySelector('div.tiptap.ProseMirror');
            const chips = ed ? [...ed.querySelectorAll('a.tiptap-topic')].map(a => {
                try { return JSON.parse(a.getAttribute('data-topic')).name; } catch (e) { return (a.innerText||'').replace(/^#/, '').replace('[话题]#',''); }
            }) : [];
            const m = (document.body.innerText || '').match(/(\\d+)\\s*\\/\\s*1000/);
            const t = document.querySelector('input[placeholder*="标题"]');
            return { text: ed ? ed.innerText : '', chips, count: m ? parseInt(m[1]) : null,
                     title: t ? t.value : null };
        }"""
    )


def verify_filled(page: Page, d: Draft, topics_chosen: List[str]) -> dict:
    st = editor_state(page)
    st["images"] = uploaded_count(page)
    problems = []
    if st["title"] != d.title:
        problems.append(f"标题不一致: {st['title']!r}")
    first_line = next((l for l in d.body.split("\n") if l.strip()), "")
    if first_line and first_line.strip()[:30] not in st["text"]:
        problems.append("正文首行未在编辑器中找到")
    if st["chips"] != topics_chosen:
        problems.append(f"话题 chip 不一致: 页面 {st['chips']}，期望 {topics_chosen}")
    if st["count"] is not None and st["count"] > 1000:
        problems.append(f"正文字数 {st['count']}/1000 超限")
    if st["images"] != len(d.images):
        problems.append(f"图片数 {st['images']}，期望 {len(d.images)}")
    if problems:
        raise XhsError("填写后自检失败：" + "；".join(problems))
    return st


# ---------------------------------------------------------------- 发布
def _publish_button_box(page: Page):
    """发布按钮在 <xhs-publish-btn> 的 closed shadow root 里，普通选择器拿不到。
    用 CDP DOM.getDocument(pierce=true) 找到其中文字为「发布」的 BUTTON，返回 (中心点, 是否禁用)。"""
    host_attrs = page.evaluate(
        """() => { const h = document.querySelector('xhs-publish-btn'); if (!h) return null;
                   const o = {}; for (const a of h.attributes) o[a.name] = a.value; return o; }""")
    if not host_attrs:
        raise XhsError("找不到 <xhs-publish-btn>（页面改版？）")
    s = page.context.new_cdp_session(page)
    try:
        doc = s.send("DOM.getDocument", {"depth": -1, "pierce": True})
        found = []

        def walk(n, inside=False):
            nm = n.get("nodeName", "")
            if nm == "XHS-PUBLISH-BTN":
                inside = True
            if inside and nm == "BUTTON":
                txt = "".join(c.get("nodeValue", "") for c in n.get("children", []) if c.get("nodeType") == 3).strip()
                if txt == host_attrs.get("submit-text", "发布"):
                    attrs = dict(zip(n.get("attributes", [])[::2], n.get("attributes", [])[1::2]))
                    found.append((n["nodeId"], attrs))
            for k in ("shadowRoots", "children"):
                for c in n.get(k, []) or []:
                    walk(c, inside)

        walk(doc["root"])
        if len(found) != 1:
            raise XhsError(f"在 xhs-publish-btn 里找到 {len(found)} 个「发布」按钮，期望 1 个")
        node_id, attrs = found[0]
        s.send("DOM.scrollIntoViewIfNeeded", {"nodeId": node_id})
        page.wait_for_timeout(300)
        q = s.send("DOM.getBoxModel", {"nodeId": node_id})["model"]["content"]
        cx = (q[0] + q[2] + q[4] + q[6]) / 4
        cy = (q[1] + q[3] + q[5] + q[7]) / 4
        disabled = (host_attrs.get("submit-disabled") == "true" or host_attrs.get("submit-loading") == "true"
                    or attrs.get("aria-disabled") == "true" or "disabled" in attrs)
        return (cx, cy), disabled, host_attrs
    finally:
        s.detach()


def find_publish_button(page: Page):
    return _publish_button_box(page)


def click_publish_once(page: Page) -> None:
    (cx, cy), disabled, _ = _publish_button_box(page)
    if disabled:
        raise XhsError("「发布」按钮当前不可点（disabled/loading），已中止")
    # 确认坐标处确实是发布按钮的宿主元素，防止点到别处
    host = page.evaluate(f"() => {{ const e = document.elementFromPoint({cx}, {cy}); return e ? e.tagName : null; }}")
    if host != "XHS-PUBLISH-BTN":
        raise XhsError(f"发布按钮被遮挡（坐标处元素是 {host}），已中止")
    page.mouse.click(cx, cy)


def is_published_url(url: str) -> bool:
    return "published=true" in url or "/publish/success" in url


def wait_published(page: Page, timeout_s: int = 40) -> str:
    end = time.time() + timeout_s
    while time.time() < end:
        if is_published_url(page.url):
            return page.url
        try:
            check_blockers(page)
        except Blocked:
            raise
        page.wait_for_timeout(1000)
    raise XhsError(f"点击发布后 {timeout_s}s 内未跳转到成功页（当前 {page.url}）。"
                   "请人工查看页面；不会再次点击。")


def note_manager_status(page: Page, title: str, timeout_s: int = 30) -> Optional[dict]:
    """打开笔记管理，找标题完全一致的第一张卡片，返回状态与时间（页面显示的是北京时间 UTC+8）。"""
    page.goto(NOTE_MANAGER_URL, wait_until="domcontentloaded", timeout=45000)
    wait_page_ready(page)
    end = time.time() + timeout_s
    while time.time() < end:
        cards = page.evaluate(
            """() => [...document.querySelectorAll('.note-card')].map(c =>
                   (c.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean))""")
        for lines in cards:
            if title in lines:
                status = next((l for l in lines if l in KNOWN_STATUSES), "已发布（无状态标签）")
                tm = next((l for l in lines if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}", l)), None)
                return {"status": status, "time": tm, "lines": lines}
        page.wait_for_timeout(1500)
    return None
