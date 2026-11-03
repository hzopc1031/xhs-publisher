"""Chrome 连接：发现 CDP 端点、映射个人资料（profile）、在指定 profile 里找/开标签页。

不读取、不导出、不打印任何 cookie / token；登录态完全复用正在运行的 Chrome。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from playwright.sync_api import Browser, Page


class BrowserError(RuntimeError):
    pass


# ---------------------------------------------------------------- 发现运行中的 Chrome
@dataclass
class ChromeProc:
    pid: int
    port: Optional[int]
    address: str
    user_data_dir: Optional[str]


def find_chrome_processes() -> List[ChromeProc]:
    """扫描 /proc，找 Chrome 主进程（没有 --type= 的那个）及其启动参数。仅 Linux。"""
    out = []
    proc = Path("/proc")
    if not proc.is_dir():
        return out
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            args = (d / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        args = [a.decode("utf-8", "replace") for a in args if a]
        if len(args) == 1 and " --" in args[0]:
            # Chrome 会改写自己的 argv，/proc/<pid>/cmdline 变成一个空格分隔的长字符串
            head, *rest = args[0].split(" --")
            args = [head] + ["--" + r.strip() for r in rest]
        if not args or not any(k in os.path.basename(args[0]) for k in ("chrome", "chromium")):
            continue
        if any(a.startswith("--type=") for a in args):
            continue
        port = addr = udd = None
        for a in args:
            if a.startswith("--remote-debugging-port="):
                try:
                    port = int(a.split("=", 1)[1])
                except ValueError:
                    pass
            elif a.startswith("--remote-debugging-address="):
                addr = a.split("=", 1)[1]
            elif a.startswith("--user-data-dir="):
                udd = a.split("=", 1)[1]
        out.append(ChromeProc(int(d.name), port, addr or "127.0.0.1", udd))
    return out


def cdp_alive(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/json/version", timeout=timeout) as r:
            return r.status == 200 and b"webSocketDebuggerUrl" in r.read()
    except Exception:
        return False


def resolve_endpoint(cdp_url: Optional[str], user_data_dir: Optional[str]):
    """返回 (cdp_url, user_data_dir)。优先显式参数/环境变量，其次自动发现。"""
    cdp_url = cdp_url or os.environ.get("XHS_CDP_URL")
    user_data_dir = user_data_dir or os.environ.get("XHS_USER_DATA_DIR")
    procs = find_chrome_processes()
    if cdp_url:
        if not user_data_dir:
            port = cdp_url.rstrip("/").rsplit(":", 1)[-1]
            for p in procs:
                if str(p.port) == port and p.user_data_dir:
                    user_data_dir = p.user_data_dir
        if not cdp_alive(cdp_url):
            raise BrowserError(f"CDP 端点不可用: {cdp_url}")
        return cdp_url, user_data_dir
    with_port = [p for p in procs if p.port]
    if user_data_dir:
        with_port = [p for p in with_port if p.user_data_dir and
                     os.path.realpath(p.user_data_dir) == os.path.realpath(user_data_dir)] or with_port
    for p in with_port:
        url = f"http://{p.address}:{p.port}"
        if cdp_alive(url):
            return url, user_data_dir or p.user_data_dir
    if cdp_alive("http://127.0.0.1:9222"):
        return "http://127.0.0.1:9222", user_data_dir
    running = [p for p in procs if not p.port]
    hint = ""
    if running:
        hint = (f"\n发现 Chrome 正在运行但没有开启远程调试（pid={running[0].pid}, "
                f"user-data-dir={running[0].user_data_dir}）。本工具不会强行重启它；"
                "请手动关闭该 Chrome 后运行 `xhs launch-chrome`，或自行带 --remote-debugging-port 启动。")
    else:
        hint = "\n没有发现运行中的 Chrome。可运行 `xhs launch-chrome --user-data-dir <目录>` 启动一个带 CDP 的 Chrome。"
    raise BrowserError("找不到可用的 CDP 端点。" + hint)


# ---------------------------------------------------------------- profile 名称映射
def read_profiles(user_data_dir: Optional[str]) -> Dict[str, str]:
    """返回 {目录名: 显示名}，来自 <user-data-dir>/Local State（只读取 profile 名称，不读其他内容）。"""
    if not user_data_dir:
        return {}
    ls = Path(user_data_dir) / "Local State"
    try:
        data = json.loads(ls.read_text(encoding="utf-8"))
    except Exception:
        return {}
    cache = data.get("profile", {}).get("info_cache", {})
    return {k: (v.get("name") or k) for k, v in cache.items()}


def resolve_profile_dir(profile: str, user_data_dir: Optional[str]) -> str:
    """把 '账号B' / 'Profile 1' / 'Default' 统一解析成目录名。"""
    profiles = read_profiles(user_data_dir)
    if profile in profiles:
        return profile
    for d, name in profiles.items():
        if name == profile:
            return d
    if profile.lower() == "default":
        return "Default"
    if profiles:
        opts = ", ".join(f"{d}「{n}」" for d, n in profiles.items())
        raise BrowserError(f"找不到 Chrome 个人资料 {profile!r}。可选：{opts}")
    # 读不到 Local State 时只能原样返回
    return profile


# ---------------------------------------------------------------- 标签页与 browserContext
def target_info(page: Page) -> dict:
    s = page.context.new_cdp_session(page)
    try:
        return s.send("Target.getTargetInfo")["targetInfo"]
    finally:
        try:
            s.detach()
        except Exception:
            pass


def default_context_id(browser: Browser) -> str:
    s = browser.new_browser_cdp_session()
    try:
        return s.send("Target.getBrowserContexts").get("defaultBrowserContextId", "")
    finally:
        s.detach()


def all_pages(browser: Browser) -> List[Page]:
    pages = []
    for c in browser.contexts:
        pages.extend(c.pages)
    return pages


def pages_with_ctx(browser: Browser):
    """[(page, targetId, browserContextId)]。连接 CDP 时 Playwright 会把多个 profile 的标签页都放进
    contexts[0]，所以用 Target.getTargetInfo 拿真实的 browserContextId 区分 profile。"""
    out = []
    for p in all_pages(browser):
        try:
            info = target_info(p)
        except Exception:
            continue
        if info.get("type") != "page":
            continue
        out.append((p, info["targetId"], info.get("browserContextId", "")))
    return out


def find_page_by_target(browser: Browser, target_id: str) -> Optional[Page]:
    for p, tid, _ in pages_with_ctx(browser):
        if tid == target_id:
            return p
    return None


def wait_new_page(browser: Browser, target_id: str, timeout: float = 10.0) -> Page:
    end = time.time() + timeout
    while time.time() < end:
        p = find_page_by_target(browser, target_id)
        if p:
            return p
        time.sleep(0.3)
    raise BrowserError("新建标签页后没能在 Playwright 中找到它")


def open_tab_in_context(browser: Browser, context_id: str, is_default: bool, url: str = "about:blank") -> Page:
    """在指定 profile（browserContext）里新开一个标签页。
    默认 profile 用 Playwright 的 new_page；其他 profile 先尝试 Target.createTarget(browserContextId)。
    失败则抛错，由调用方决定是否复用该 profile 已有的标签页。"""
    if is_default:
        return browser.contexts[0].new_page()
    s = browser.new_browser_cdp_session()
    try:
        r = s.send("Target.createTarget", {"url": url, "browserContextId": context_id})
    except Exception as e:
        raise BrowserError(f"无法在该 profile 中新建标签页: {e}") from e
    finally:
        s.detach()
    return wait_new_page(browser, r["targetId"])


# ---------------------------------------------------------------- 启动带 CDP 的 Chrome（可选模式）
def chrome_binary() -> str:
    for c in (os.environ.get("XHS_CHROME"), "/opt/google/chrome/chrome", shutil.which("google-chrome"),
              shutil.which("google-chrome-stable"), shutil.which("chromium"), shutil.which("chromium-browser")):
        if c and os.path.exists(c):
            return c
    raise BrowserError("找不到 Chrome 可执行文件，可用环境变量 XHS_CHROME 指定")


def launch_chrome(user_data_dir: str, port: int = 9222, profile_dir: Optional[str] = None) -> str:
    """以独立进程启动 Chrome（带 --remote-debugging-port），工具退出后 Chrome 仍保持运行。
    若该 user-data-dir 已被其他 Chrome 占用则拒绝（不会杀进程/不会重启）。"""
    udd = os.path.realpath(os.path.expanduser(user_data_dir))
    for p in find_chrome_processes():
        if p.user_data_dir and os.path.realpath(p.user_data_dir) == udd:
            if p.port and cdp_alive(f"http://{p.address}:{p.port}"):
                return f"http://{p.address}:{p.port}"
            raise BrowserError(f"该 user-data-dir 已被运行中的 Chrome 占用（pid={p.pid}）且未开远程调试；"
                               "本工具不会重启它，请先手动关闭。")
    args = [chrome_binary(), f"--user-data-dir={udd}", f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check"]
    if profile_dir:
        args.append(f"--profile-directory={profile_dir}")
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        if cdp_alive(url):
            return url
        time.sleep(0.5)
    raise BrowserError("Chrome 已启动但 CDP 端点 30 秒内未就绪")
