# xhs-publisher

小红书（创作服务平台 creator.xiaohongshu.com）**图文笔记**命令行工具：

- `xhs fill`：校验草稿 → 确认账号昵称 → 上传图片、填标题、正文、话题 → 截一张图 → **停在发布前**，标签页保持打开
- `xhs publish`：复核同一个标签页（账号、标题、图片、话题）→ **只点一次「发布」** → 去「笔记管理」查状态
- 填写与发布是两个独立命令，中间可以人工检查截图或页面。

工具通过 **Chrome DevTools Protocol（CDP）连接你已经登录的 Chrome**，直接复用现有登录态：
不读取、不导出、不打印 cookie / token，不会登出，也不会触发重新登录。

## 安装

```bash
cd xhs-publisher
python3 -m venv .venv
.venv/bin/pip install -e .
# 只用 CDP 连接现有 Chrome 时，不需要 `playwright install`
```

之后用 `.venv/bin/xhs ...`（或激活 venv 后直接 `xhs ...`）。也可以 `python -m xhs_publisher ...`。

## 前提：Chrome 开着远程调试端口

工具会自动扫描本机 Chrome 主进程的启动参数，找到 `--remote-debugging-port` 和 `--user-data-dir`；
也可以手动指定 `--cdp-url http://127.0.0.1:9222`（或环境变量 `XHS_CDP_URL`）。

如果 Chrome 在运行但**没有**开远程调试，工具**不会**杀掉或重启它，只会报错提示。
你可以手动关掉 Chrome 后让工具启动一个带 CDP 的独立 Chrome（工具退出后它继续运行）：

```bash
xhs launch-chrome --user-data-dir ~/.config/google-chrome --port 9222
```

查看有哪些个人资料、各标签页属于哪个 profile、登录的昵称（只读）：

```bash
xhs profiles
```

## 个人资料（profile）与账号

`--profile` 可以写 Chrome 里显示的名字（如 `账号B`），也可以写目录名（`Default`、`Profile 1`），
名字和目录的对应关系读取自 `<user-data-dir>/Local State`（只读取 profile 名称）。

`--expect-nickname` 是**安全闸**：页面右上角昵称与之不一致时立即中止，什么都不填。
可以把常用对应关系写进 `profiles.json`（已被 .gitignore 忽略），之后可省略 `--expect-nickname`：

```bash
cp examples/profiles.example.json profiles.json
```

> 说明：CDP 能直接认出 `Default` profile；其他 profile 只能通过「该 profile 已打开的创作平台标签页的昵称」来识别。
> 所以使用非默认 profile 前，请确保它的窗口里开着任意一个 creator.xiaohongshu.com 页面（例如笔记管理）。

## 用法

### 1. 填写（不发布）

```bash
xhs fill --profile Default --expect-nickname 示例昵称A \
  --title "测试草稿勿发" \
  --body-file body.txt \
  --images a.png b.png \
  --topics 话题一 话题二 话题三
```

或用 JSON 草稿（相对路径相对于 JSON 文件所在目录；命令行参数会覆盖 JSON）：

```bash
xhs fill --profile 账号B --expect-nickname 示例昵称B --input draft.json
```

```json
{
  "title": "标题（≤20字）",
  "body": "正文……（也可以用 \"body_file\": \"body.txt\"）",
  "images": ["cover.png", "2.png"],
  "topics": ["话题一", "话题二", "话题三"]
}
```

校验规则（不通过直接退出，不打开浏览器）：

| 项目 | 规则 |
| --- | --- |
| 标题 | 1–20 字 |
| 正文 | 非空；正文 + 话题 ≤ 1000 字（填写后还会读页面的 `N/1000` 再核对一次） |
| 图片 | 1–18 张，文件存在，jpg/jpeg/png/webp |
| 话题 | 3–5 个，不重复（写不写 `#` 都行） |

只校验不打开浏览器：加 `--validate-only`。

填写过程：
1. 新开一个标签页（非默认 profile 若无法新开，则复用该 profile 里**非编辑中**的创作平台标签页）打开发布页；
2. 检查登录页/二维码/验证码/风控提示（如「登录超限」），有就中止；
3. 核对昵称；
4. 切到「上传图文」，通过 file input 上传图片，等待 `N/18` 计数一致；
5. 标题用 `fill`，正文用 `insertText`（不用逐键输入，中文不会乱码），换行生成新段落；
6. 话题：在正文末尾输入 `#话题名`，从下拉里点选**完全一致**的项，变成真正的话题 chip；
   没有完全一致的项默认报错（`--topic-fallback first` 可改为选第一项）；
7. 自检（标题、正文、chip、字数、图片数）→ 截图 → 写入 `state/<profile>.json` → 退出，标签页保持打开。

### 2. 发布

```bash
xhs publish --profile Default --expect-title "测试草稿勿发"           # 真发布
xhs publish --profile Default --expect-title "测试草稿勿发" --dry-run # 只复核、定位按钮，不点击
```

发布前复核：fill 记录存在且状态为 `filled`、`--expect-title` 与记录及页面标题一致、昵称一致、仍在编辑页、
有图片、话题 chip 与记录一致、无拦截提示、发布按钮可点。然后**只点一次**「发布」
（点击前先把状态写成 `publish_clicked`，之后任何重试都会被拒绝，防止重复发布）。
等待跳转到 `published=true` 或 `/publish/success`，再打开「笔记管理」查找同名笔记，打印状态（如 `审核中`）和时间，截一张图。

> 笔记管理里显示的时间是北京时间（UTC+8）。

### 3. 其他

```bash
xhs discard --profile Default --close   # 丢弃已填写的内容（离开编辑页，不保存草稿），并关闭 fill 新开的标签页
xhs status  --profile Default --title "某标题"   # 在笔记管理里查状态（新开标签页查询后关闭）
```

### 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 2 | 参数/草稿校验失败 |
| 3 | 页面操作失败（找不到元素、自检不通过、标签页不存在等） |
| 4 | 被拦截：登录页、二维码、验证码、风控、登录超限 |
| 5 | 账号昵称不符 |

## 文件

```
xhs_publisher/
  cli.py      命令行入口与流程
  browser.py  CDP 发现、profile 映射、标签页管理、launch-chrome
  xhs.py      小红书页面选择器与操作（页面改版时主要改这里）
  draft.py    草稿读取与校验
  state.py    state/ 目录下的状态文件与截图路径
examples/     草稿与 profiles.json 示例
state/        运行时生成（已忽略，不要提交）
```

状态目录默认是当前目录下的 `state/`，可用环境变量 `XHS_STATE_DIR` 修改。状态文件只包含标签页 id、标题、话题等，不含任何登录信息。

## 已知限制 / 易变的地方

- **选择器依赖页面结构**，小红书改版就可能失效，集中在 `xhs_publisher/xhs.py` 顶部：
  - 昵称：`.user-info .name-box`
  - 「上传图文」：页面有多个 `.creator-tab`，其中一个被放到屏幕外（-9999px），工具只点视口内的那个
  - 图片 input：`input[type=file][accept*=png]`；上传完成判断依赖文字「图片编辑 N/18」
  - 标题：`input[placeholder*="标题"]`；正文：`div.tiptap.ProseMirror[contenteditable=true]`
  - 话题下拉：`#creator-editor-topic-container div.item span.name`；话题 chip：`a.tiptap-topic`
  - **发布按钮在 `<xhs-publish-btn>` 的 closed shadow root 里**，普通选择器拿不到；
    工具用 CDP `DOM.getDocument(pierce=true)` 找到文字为「发布」的按钮，取坐标后用鼠标点击，
    并先确认该坐标处的元素就是 `xhs-publish-btn`
  - 笔记管理：`.note-card`，状态取卡片里的「审核中 / 未通过 …」文字
- 拦截检测基于关键词（如「登录超限」「扫码登录」「安全验证」「滑块」）和 URL（`/login`、`captcha`），可能漏判或误判；
  若标题/正文本身包含这些词，可能误报。
- 非默认 profile 无法通过 CDP 直接从目录名定位，需要该 profile 已开着创作平台页面并按昵称识别；
  Chrome 不允许在非默认 profile 里通过 CDP 新建标签页时，会复用该 profile 中**不在编辑**的创作平台标签页。
- 旧标签页里的昵称可能是改名前的（页面没刷新），fill 会重新加载发布页后再核对，以新页面为准。
- 图片顺序按命令行/JSON 顺序一次性上传，未单独验证上传后的顺序。
- 只支持图文笔记；不支持视频、长文、定时发布、封面编辑、@用户、地点、合集等。
- 不会点「暂存离开」，不会保存草稿；不会点赞/评论/关注/删除任何内容。
- 仅在 Linux 下测试（自动发现 Chrome 依赖 `/proc`；其他系统请用 `--cdp-url`）。

## 安全约定

- 绝不提交 `state/`、截图、`profiles.json`、任何浏览器数据（见 `.gitignore`）。
- 工具不会读取 cookie 文件或 Local Storage，只读取 `Local State` 里的 profile 显示名。
- 发布操作只在 `xhs publish` 中、且每次 fill 最多点击一次。

## License

MIT
