"""命令行入口：xhs fill / publish / discard / status / profiles / launch-chrome"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import browser as B
from . import state as S
from . import xhs as X
from .draft import DraftError, load_draft

EXIT_OK, EXIT_USAGE, EXIT_PAGE, EXIT_BLOCKED, EXIT_ACCOUNT = 0, 2, 3, 4, 5
log = X.log


# ---------------------------------------------------------------- 配置
def load_profiles_config() -> dict:
    for p in (Path.cwd() / "profiles.json", Path(__file__).resolve().parent.parent / "profiles.json"):
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


def expected_nickname(args, profile_dir: str, profile_name: str) -> str:
    if args.expect_nickname:
        return args.expect_nickname
    cfg = load_profiles_config()
    for k in (args.profile, profile_dir, profile_name):
        if k in cfg and cfg[k].get("nickname"):
            return cfg[k]["nickname"]
    raise DraftError("必须提供 --expect-nickname（或在 profiles.json 中配置该 profile 的 nickname）")


# ---------------------------------------------------------------- 选 profile 对应的 browserContext
def select_context(browser, profile_dir: str, expect_nick: str):
    """返回 (context_id, is_default)。
    CDP 的默认 browserContext 不一定对应 Default 目录（取决于哪个 profile 窗口先打开），
    因此遍历每个 context 中已打开的 creator.xiaohongshu.com 标签页，按页面昵称匹配。
    is_default 表示命中的 context 是否为 defaultBrowserContextId。"""
    # 注意：CDP 的默认 browserContext 不一定对应 Default 目录（取决于哪个 profile 窗口先打开），
    # 所以一律按页面昵称匹配，不按 context 是否默认来推断账号。
    default_id = B.default_context_id(browser)
    seen = {}
    for page, _tid, ctx in B.pages_with_ctx(browser):
        if X.CREATOR_HOST not in page.url:
            continue
        try:
            nick = page.locator(X.SEL_NICKNAME).first.inner_text(timeout=3000).strip()
        except Exception:
            nick = "?"
        seen.setdefault(ctx, set()).add(nick)
        if nick == expect_nick:
            return ctx, ctx == default_id
    detail = "; ".join(f"{c[:8]}: {sorted(n)}" for c, n in seen.items()) or "无"
    raise X.XhsError(
        f"按页面昵称在所有 profile 中找不到「{expect_nick}」的小红书创作平台标签页（已发现: {detail}）。\n"
        f"CDP 无法把 profile 目录 {profile_dir!r} 直接映射到标签页，请先在该 profile 的 Chrome 窗口里"
        f"打开/刷新 {X.NOTE_MANAGER_URL}，再重试。")


def open_work_tab(browser, ctx_id: str, is_default: bool, reuse_tab: bool):
    """返回 (page, created)。优先新开标签页；非默认 profile 若不能新开，复用该 profile 里的创作平台标签页。"""
    if not reuse_tab:
        try:
            return B.open_tab_in_context(browser, ctx_id, is_default), True
        except B.BrowserError as e:
            log(f"{e}；改为复用该 profile 已有的创作平台标签页")
    cands = [(p, tid) for p, tid, c in B.pages_with_ctx(browser) if c == ctx_id and X.CREATOR_HOST in p.url]
    # 不抢正在编辑的发布页，除非显式 --reuse-tab
    safe = [c for c in cands if "/publish/publish" not in c[0].url]
    pick = (safe or (cands if reuse_tab else []))
    if not pick:
        raise X.XhsError("该 profile 没有可复用的创作平台标签页（只有正在编辑的发布页）。"
                         "如确认可以覆盖，请加 --reuse-tab。")
    return pick[0][0], False


def connect(pw, args):
    url, udd = B.resolve_endpoint(args.cdp_url, args.user_data_dir)
    log(f"连接 Chrome CDP: {url}  (user-data-dir={udd})")
    return pw.chromium.connect_over_cdp(url), udd


def profile_names(args, udd):
    pdir = B.resolve_profile_dir(args.profile, udd)
    pname = B.read_profiles(udd).get(pdir, pdir)
    return pdir, pname


# ---------------------------------------------------------------- fill
def cmd_fill(args) -> int:
    d = load_draft(args.input, args.title, args.body, args.body_file, args.images, args.topics)
    log(f"草稿校验通过：标题 {len(d.title)} 字，正文 {len(d.body)} 字，图片 {len(d.images)} 张，话题 {d.topics}")
    if args.validate_only:
        return EXIT_OK
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser, udd = connect(pw, args)
        pdir, pname = profile_names(args, udd)
        nick = expected_nickname(args, pdir, pname)
        prev = S.load(pdir)
        if prev and prev.get("status") == "filled" and not args.force:
            old = B.find_page_by_target(browser, prev["target_id"])
            if old and "/publish/publish" in old.url:
                raise X.XhsError(f"该 profile 已有一个已填写未发布的标签页（标题「{prev['title']}」）。"
                                 "先 `xhs publish` 或 `xhs discard`，或加 --force。")
        ctx_id, is_default = select_context(browser, pdir, nick)
        page, created = open_work_tab(browser, ctx_id, is_default, args.reuse_tab)
        tid = B.target_info(page)["targetId"]
        shot = S.screenshot_path("fill", pdir)
        try:
            page.bring_to_front()
            X.goto_publish_image(page)
            real = X.verify_nickname(page, nick)
            log(f"账号确认：{real}（profile {pdir}「{pname}」）")
            X.click_image_tab(page)
            X.upload_images(page, d.images)
            log(f"图片已上传 {len(d.images)} 张")
            X.fill_title(page, d.title)
            X.fill_body(page, d.body)
            chosen = X.add_topics(page, d.topics, args.topic_fallback)
            st = X.verify_filled(page, d, chosen)
            X.check_blockers(page)
            page.mouse.move(5, 5)
            page.screenshot(path=shot)
        except Exception:
            try:
                page.screenshot(path=shot.replace("-fill.png", "-fill-error.png"))
                log(f"出错截图: {shot.replace('-fill.png', '-fill-error.png')}")
            except Exception:
                pass
            if created:   # 出错时关掉本次新建的标签页，未保存内容直接丢弃
                try:
                    page.close(run_before_unload=False)
                    log("已关闭本次新建的标签页（内容未保存）")
                except Exception:
                    pass
            raise
        S.save(pdir, {
            "status": "filled", "profile": args.profile, "profile_dir": pdir, "profile_name": pname,
            "nickname": real, "context_id": ctx_id, "target_id": tid, "created_tab": created,
            "title": d.title, "images": d.images, "topics": chosen, "body_chars": st.get("count"),
            "filled_at": datetime.now().astimezone().isoformat(timespec="seconds"), "screenshot": shot,
        })
        log(f"填写完成（未发布）。正文计数 {st.get('count')}/1000，话题 {st['chips']}")
        log(f"截图: {shot}")
        log(f"标签页保持打开。确认无误后运行: xhs publish --profile {args.profile!r} --expect-title {d.title!r}")
    return EXIT_OK


# ---------------------------------------------------------------- publish
def _get_state_page(browser, pdir):
    st = S.load(pdir)
    if not st:
        raise X.XhsError(f"没有找到 profile {pdir} 的填写记录，请先运行 xhs fill")
    page = B.find_page_by_target(browser, st["target_id"])
    if not page:
        raise X.XhsError("fill 打开的标签页已不存在（被关闭或 Chrome 重启），请重新 fill")
    return st, page


def cmd_publish(args) -> int:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser, udd = connect(pw, args)
        pdir, pname = profile_names(args, udd)
        st, page = _get_state_page(browser, pdir)
        if st.get("status") != "filled":
            raise X.XhsError(f"状态为 {st.get('status')!r}，不是 filled；为防止重复发布，拒绝执行。")
        if args.expect_title != st["title"]:
            raise X.XhsError(f"--expect-title 与 fill 记录不一致：记录为「{st['title']}」")
        nick = args.expect_nickname or st["nickname"]
        if "/publish/publish" not in page.url or X.is_published_url(page.url):
            raise X.XhsError(f"标签页已不在发布编辑页: {page.url}")
        page.bring_to_front()
        X.check_blockers(page)
        X.verify_nickname(page, nick)
        es = X.editor_state(page)
        if es["title"] != args.expect_title:
            raise X.XhsError(f"页面标题「{es['title']}」与期望「{args.expect_title}」不一致，已中止")
        if X.uploaded_count(page) in (None, 0):
            raise X.XhsError("页面上没有已上传的图片，已中止")
        if es["chips"] != st.get("topics"):
            raise X.XhsError(f"话题 chip {es['chips']} 与记录 {st.get('topics')} 不一致，已中止")
        (_cx, _cy), disabled, _ = X.find_publish_button(page)
        log(f"复核通过：账号 {nick}，标题「{es['title']}」，图片 {X.uploaded_count(page)} 张，话题 {es['chips']}，"
            f"发布按钮{'不可点' if disabled else '可点'}")
        if args.dry_run:
            log("--dry-run：不点击发布。")
            return EXIT_OK
        # 先落盘再点击：即使后续出错也不会被再次发布
        st.update(status="publish_clicked", publish_clicked_at=datetime.now().astimezone().isoformat(timespec="seconds"))
        S.save(pdir, st)
        X.click_publish_once(page)
        log("已点击「发布」（仅一次）")
        shot = S.screenshot_path("published", pdir)
        try:
            url = X.wait_published(page)
            log(f"发布成功跳转: {url}")
            info = X.note_manager_status(page, st["title"])
        except Exception:
            try:
                page.screenshot(path=shot)
                log(f"截图: {shot}")
            except Exception:
                pass
            raise
        page.screenshot(path=shot)
        st.update(status="published", published_url=url, note_manager=info, screenshot_published=shot)
        S.save(pdir, st)
        if info:
            log(f"笔记管理：「{st['title']}」 状态={info['status']} 时间={info['time']}（页面时间为北京时间 UTC+8）")
        else:
            log("笔记管理里暂未找到该标题（可能列表还没刷新），请稍后运行 xhs status")
        log(f"截图: {shot}")
    return EXIT_OK


# ---------------------------------------------------------------- discard / status / profiles / launch
def cmd_discard(args) -> int:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser, udd = connect(pw, args)
        pdir, _ = profile_names(args, udd)
        st, page = _get_state_page(browser, pdir)
        if st.get("status") != "filled":
            log(f"状态为 {st.get('status')}，无需丢弃")
            return EXIT_OK
        page.on("dialog", lambda dlg: dlg.accept())   # 「离开页面」确认框：接受=丢弃
        page.goto(X.NOTE_MANAGER_URL, wait_until="domcontentloaded", timeout=45000)
        log(f"已离开编辑页，未保存的内容已丢弃（当前 {page.url}）")
        if args.close and st.get("created_tab"):
            page.close(run_before_unload=False)
            log("已关闭 fill 新建的标签页")
        st.update(status="discarded")
        S.save(pdir, st)
    return EXIT_OK


def cmd_status(args) -> int:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser, udd = connect(pw, args)
        pdir, pname = profile_names(args, udd)
        st = S.load(pdir) or {}
        title = args.title or st.get("title")
        if not title:
            raise DraftError("请提供 --title")
        page = B.find_page_by_target(browser, st["target_id"]) if st.get("target_id") else None
        if page and "/publish/publish" in page.url and not X.is_published_url(page.url) and st.get("status") == "filled":
            raise X.XhsError("记录中的标签页还在编辑未发布内容，为避免丢失不在此标签页查询；请先 publish 或 discard")
        created = False
        if not page:
            nick = expected_nickname(args, pdir, pname)
            ctx_id, is_default = select_context(browser, pdir, nick)
            page, created = open_work_tab(browser, ctx_id, is_default, False)
        try:
            info = X.note_manager_status(page, title, timeout_s=15)
            if args.expect_nickname or st.get("nickname"):
                X.verify_nickname(page, args.expect_nickname or st["nickname"])
        finally:
            if created:
                page.close()
        if info:
            log(f"「{title}」 状态={info['status']} 时间={info['time']}（北京时间 UTC+8）")
        else:
            log(f"笔记管理首屏没有找到「{title}」")
    return EXIT_OK


def cmd_profiles(args) -> int:
    from playwright.sync_api import sync_playwright
    url, udd = B.resolve_endpoint(args.cdp_url, args.user_data_dir)
    print(f"CDP: {url}\nuser-data-dir: {udd}")
    for d, n in B.read_profiles(udd).items():
        print(f"  profile 目录 {d!r:14} 名称「{n}」")
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(url)
        default_id = B.default_context_id(browser)
        for page, tid, ctx in B.pages_with_ctx(browser):
            nick = ""
            if X.CREATOR_HOST in page.url:
                try:
                    nick = page.locator(X.SEL_NICKNAME).first.inner_text(timeout=2000).strip()
                except Exception:
                    nick = "?"
            tag = "Default" if ctx == default_id else ctx[:8]
            print(f"  [{tag:8}] tab {tid[:8]} {page.url[:80]}" + (f"  昵称={nick}" if nick else ""))
    return EXIT_OK


def cmd_launch(args) -> int:
    udd = args.user_data_dir
    if not udd:
        raise DraftError("launch-chrome 需要 --user-data-dir")
    pdir = B.resolve_profile_dir(args.profile, udd) if args.profile else None
    url = B.launch_chrome(udd, args.port, pdir)
    log(f"Chrome CDP 就绪: {url}（之后命令加 --cdp-url {url}，或设置 XHS_CDP_URL）")
    return EXIT_OK


# ---------------------------------------------------------------- argparse
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="xhs", description="小红书图文笔记：填写（不发布）与发布分离的命令行工具")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cdp-url", help="Chrome 远程调试地址，如 http://127.0.0.1:9222（默认自动发现，或环境变量 XHS_CDP_URL）")
    common.add_argument("--user-data-dir", help="Chrome user-data-dir（默认从运行中的 Chrome 进程参数自动读取）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fill", parents=[common], help="填写图文笔记，停在发布前")
    f.add_argument("--profile", required=True, help="Chrome 个人资料名或目录名，如 Default / 账号B / 'Profile 1'")
    f.add_argument("--expect-nickname", help="期望的小红书昵称，不一致则中止")
    f.add_argument("--input", help="草稿 JSON：{title, body|body_file, images[], topics[]}")
    f.add_argument("--title")
    g = f.add_mutually_exclusive_group()
    g.add_argument("--body")
    g.add_argument("--body-file")
    f.add_argument("--images", nargs="+")
    f.add_argument("--topics", nargs="+", help="3-5 个话题，不带 #")
    f.add_argument("--topic-fallback", choices=["fail", "first"], default="fail",
                   help="下拉里没有完全一致的话题时：fail=报错（默认）；first=选第一项")
    f.add_argument("--reuse-tab", action="store_true", help="允许复用该 profile 已有的发布编辑页（会覆盖其内容）")
    f.add_argument("--force", action="store_true", help="即使该 profile 已有未发布的 fill 记录也继续")
    f.add_argument("--validate-only", action="store_true", help="只校验草稿，不打开浏览器")
    f.set_defaults(func=cmd_fill)

    p = sub.add_parser("publish", parents=[common], help="复核 fill 打开的标签页并点击一次「发布」")
    p.add_argument("--profile", required=True)
    p.add_argument("--expect-title", required=True, help="必须与 fill 时的标题完全一致")
    p.add_argument("--expect-nickname", help="默认使用 fill 时确认过的昵称")
    p.add_argument("--dry-run", action="store_true", help="只复核、定位发布按钮，不点击")
    p.set_defaults(func=cmd_publish)

    d = sub.add_parser("discard", parents=[common], help="丢弃 fill 填写的内容（离开编辑页，不保存草稿）")
    d.add_argument("--profile", required=True)
    d.add_argument("--close", action="store_true", help="同时关闭 fill 新建的标签页")
    d.set_defaults(func=cmd_discard)

    s = sub.add_parser("status", parents=[common], help="在笔记管理里查某标题的状态")
    s.add_argument("--profile", required=True)
    s.add_argument("--title", help="默认用最近一次 fill 的标题")
    s.add_argument("--expect-nickname")
    s.set_defaults(func=cmd_status)

    pr = sub.add_parser("profiles", parents=[common], help="列出 Chrome 个人资料与各标签页（只读）")
    pr.set_defaults(func=cmd_profiles)

    lc = sub.add_parser("launch-chrome", help="启动一个带远程调试端口的 Chrome（不会重启已在运行的 Chrome）")
    lc.add_argument("--user-data-dir", required=True)
    lc.add_argument("--port", type=int, default=9222)
    lc.add_argument("--profile", help="启动时打开的个人资料")
    lc.set_defaults(func=cmd_launch, cdp_url=None)
    return ap


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or EXIT_OK
    except DraftError as e:
        print(f"[xhs] 参数/草稿错误: {e}", file=sys.stderr)
        return EXIT_USAGE
    except X.Blocked as e:
        print(f"[xhs] 已中止（被拦截）: {e}", file=sys.stderr)
        return EXIT_BLOCKED
    except X.XhsError as e:
        code = EXIT_ACCOUNT if "账号不符" in str(e) else EXIT_PAGE
        print(f"[xhs] 已中止: {e}", file=sys.stderr)
        return code
    except B.BrowserError as e:
        print(f"[xhs] 浏览器错误: {e}", file=sys.stderr)
        return EXIT_PAGE
