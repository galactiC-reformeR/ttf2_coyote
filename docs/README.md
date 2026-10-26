# ttf2_coyote 宣发站

《泰坦陨落 2》郊狼联动插件（`ttf2_coyote` v2.20.5）的单页宣发站。
纯静态：没有构建步骤、没有 npm 依赖、没有外链字体或 CDN 请求。

## 打开方式

双击 `index.html` 即可，`file://` 下也能正常跑（脚本、样式、动画都是本地相对路径）。

想在局域网/手机上预览时，在本目录起一个静态服务：

```powershell
# 任选其一
python -m http.server 8080
npx serve .
```

然后打开 `http://localhost:8080`。

仓库里自带一个零依赖的 node 静态服务（`serve.mjs`），也可以用：

```powershell
node serve.mjs 8123          # 只允许本机访问
node serve.mjs 8123 0.0.0.0  # 允许同网段设备访问（手机看效果用）
```

## 临时公网地址（隧道）

本机没有公网 IP，要让外人打开就得从这台机器打隧道出去。下面三种都不需要注册账号，按稳定性排序：

```powershell
# 1) 先起本地静态服务
node serve.mjs 8123

# 2) 再起隧道（任选其一，都会打印一个 https 网址）
#    A. Cloudflare 快速隧道——最稳，走 Cloudflare 边缘，实测境外节点可直接打开
& '..\tools\cloudflared\cloudflared.exe' tunnel --no-autoupdate --url http://127.0.0.1:8123
#    B. localhost.run——轻量，但会掉线（掉线时访问返回 502）
ssh -o StrictHostKeyChecking=no -R 80:127.0.0.1:8123 nokey@localhost.run
#    C. serveo——首次打开会先看到一个提示页
ssh -o StrictHostKeyChecking=no -R 80:127.0.0.1:8123 serveo.net
```

注意事项：

- 网址都是随机生成的，**隧道一断就失效**；本机睡眠、断网、关掉进程都会断，重开会拿到新网址。
- 三条隧道实测稳定性差很多：`localhost.run` 与 `serveo` 各掉过两次，Cloudflare 那条一直在线。
- 想要固定网址，只能托管到静态平台（见下一节），或给隧道服务加账号换固定域名。

## 目录结构

```
docs/                      ← GitHub Pages 的发布根（仓库主分支的 docs 目录）
├─ index.html            页面结构与全部文案
├─ assets/css/site.css   配色、排版、响应式、动效
├─ assets/js/site.js     导航吸顶、滚动出现、终端演练动画、复制、折叠面板
├─ serve.mjs             零依赖静态服务（本地预览 / 隧道对外）
└─ _preview/             桌面端与移动端截图（可删）
```

## 部署

整份目录原样上传即可，不需要任何环境变量或服务端逻辑。

- **本仓库**：主分支 `main` 的 `docs/` 目录就是发布根，GitHub Pages 已指向它
  （`Settings → Pages → Source = Deploy from a branch / main / /docs`）。
  推上去即生效，网址形如 `https://galactiC-reformeR.github.io/ttf2_coyote/`。
- **Cloudflare Pages / Vercel / Netlify**：把本目录作为发布根，构建命令留空，输出目录填 `/`（或 `./`）。
- **自有服务器**：拷到 Nginx/Apache 的站点目录即可；`index.html` 是唯一入口。

页面里没有绝对路径，放在子目录（例如 `https://example.com/ttf2/`）下也能正常工作。

## 文案的事实来源

站上出现的每一个数字都取自插件自身的资料，改动时请同步回源，不要凭印象写：

| 站点内容 | 来源 |
|---|---|
| 版本号 v2.20.5、名称「泰坦陨落2 联动」、作者 | `../manifest.json` |
| 档位强度 20 / 30 / 40 / 60、受击 25、死亡 45 | `manifest.json` 默认值 |
| 受击门槛 1500 / 1 秒累计 2000、时长 300ms、不应期 200ms | `manifest.json` 与 `../README.md` |
| 死亡脉冲 5 秒、0.5–15 秒可调、500ms 去抖 | `README.md`「濒死判据」「两个必须知道的读法陷阱」 |
| 参数面板六组字段与默认值 | `manifest.json` 的 `config_schema` |
| 内置地址与 `engine.dll` 校验值 | `README.md`「内置定位的验证记录」 |
| 17 个黄金场景、25 个会话用例、13 个自检用例 | `../README.md`「自测」 |
| 面板可选 5 个剧本（R1/S1/S2/S4/S10）、内置 17 个 | `manifest.json` 的 `sim_scenario` 与 `../testvectors/scenarios.json` |
| 进程候选、上限换算、重连自愈、fail-closed | `README.md` 对应小节 |

插件本体更新后，需要一起改的地方：

1. 导航右侧与页脚的版本号（搜 `v2.20.5`）。
2. 参数面板折叠区的字段与默认值（`#config` 里六组 `<details>`）。
3. 版本轨迹（`#changelog`）按新版本补一条，旧条目不要删。
4. 内置地址变了的话，一起改安装区的「内置定位依据」。

## 交互说明

- 首屏终端是一次**加速演示**：剧本取自插件自带的 `S1`（68% → 受击 → 48% → 受击 → 29% → 濒死 → 死亡界面），日志格式与插件真实输出一致，时间戳被压缩过。要改演示内容改 `assets/js/site.js` 顶部的 `SCRIPT` 数组。
- 滚动出现用几何判定实现（`revealPass`），不依赖 `IntersectionObserver`，所以直接跳到 `#install`、打印成 PDF、或截长图都不会出现空白段落。
- 折叠面板每组只允许展开一个，点新的会自动收起上一个。
- 「复制路径」按钮在 `file://` 下用 `execCommand` 兜底，`http(s)://` 下走 `navigator.clipboard`。

## 改风格

配色集中在 `assets/css/site.css` 顶部的 `:root`：

```css
--orange: #ff7a1a;   /* 主强调色，档位、脉冲、按钮 */
--cyan:   #4dd2ff;   /* 次强调色，A 通道与数据流 */
--red:    #ff5a5a;   /* 死亡脉冲 */
--bg:     #05080d;   /* 页面底色 */
```

字号与间距都走 `clamp()` 或统一变量，改一处即可整体缩放。

## 声明

页面文案按「成年用户自用工具」定位撰写：只读游戏内存、不注入、不读屏，不鼓励在受限对局中使用。
电刺激相关部分只描述插件行为（强度值、时长、急停），不涉及使用指导以外的内容。改文案时请保持这个尺度。
