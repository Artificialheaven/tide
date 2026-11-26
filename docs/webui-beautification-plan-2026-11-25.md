# Web 管理面板美化方案书（2026-11-25）

> **文档性质**：设计方案，不含实现代码（配套可点击的静态视觉稿见 `docs/webui-mockup-2026-11-25.html`）。
> **代码基线**：`docs/audit-2026-11-25` 分支，HEAD `164262e`。
> **关联文档**：`docs/ux-review-2026-11-25.md`（本方案吸收其中 U-10/U-13/U-14/U-16/U-17/U-30～U-34/U-38 的界面侧诉求，不改其产品行为结论）。
> **约束**：不引入 npm / 构建链 / 前端框架；Flask 直接渲染；全程离线可用；后端 API 路径与语义保持不变。

---

## 0. 结论先行

当前 Web 界面不是"丑"，而是**没有体系**：框架面板和 deepseek 插件页是两套色板、两套字体、两套按钮、两套导航，8 个插件页各自内联一段 `<style>`、复制一份顶部导航，加在一起 9 份样式、8 份导航，改一处颜色要改 9 个文件。

本方案要做三件事：

1. **一套 token**：颜色/字号/间距/圆角/阴影全部收敛到 CSS 变量，一处改动全局生效；
2. **一个外壳**：左侧栏 + 顶栏的 App Shell，框架 5 个 tab 与插件 7 个入口共用同一导航，用户不用记两套入口；
3. **一套组件**：按钮、表单、表格、卡片、图表、日志台、Toast、Modal 等 20 个组件统一规格，替换现在的 emoji 图标和三种绿色按钮。

落地方式为"新增静态资源 + 模板继承"，渐进替换、随时可回滚，**不动任何一条后端路由**。预计 5 个阶段（M0–M4）、约 17 人日。

---

## 1. 现状盘点

### 1.1 页面清单

| # | 页面 | 文件 | 体量 | 现状形态 |
|---|---|---|---|---|
| 1 | 框架面板（5 tab） | `ui/web/templates/index.html` | 639 行 | 登录页 + 顶栏 + 横向 tab |
| 2 | 群列表 | `plugins/deepseek/templates/dashboard.html` | 68 行 | 顶部链接导航 + 表格 |
| 3 | 群设置 | `plugins/deepseek/templates/group.html` | 353 行 | 6 个 fieldset 一长条 |
| 4 | 全局设置 | `plugins/deepseek/templates/settings.html` | 435 行 | 24 个 fieldset 一长条 |
| 5 | 群友记忆 | `plugins/deepseek/templates/memory.html` | 162 行 | 两张表，无分页 |
| 6 | MCP 工具 | `plugins/deepseek/templates/mcp.html` | 100 行 | 表格 + JSON textarea |
| 7 | 后台/轮询 | `plugins/deepseek/templates/runs.html` | 355 行 | 三段表单 + 表格 |
| 8 | 提示词 | `plugins/deepseek/templates/prompts.html` | 162 行 | 卡片 + textarea |
| 9 | B 站直播 | `plugins/deepseek/templates/bili_live.html` | 457 行 | 长表单 |
| 10 | 弹幕机（OBS） | `plugins/deepseek/templates/live/danmaku.html` | 22 KB | 全屏透明浮层 |
| 11 | 调试台 | `plugins/deepseek/templates/live/debug.html` | 17 KB | 全屏工具页 |

关键点：**2～9 号页面是 8 个互相独立的整页 HTML**，不是框架的一部分（观察页另算）。每份都自带 `<style>` 和顶部导航，改品牌、改色、加一个菜单项 = 改 8 遍。

### 1.2 两套视觉体系

| 维度 | 框架面板 | deepseek 插件页 | 冲突后果 |
|---|---|---|---|
| 背景 | `#0f1117`（近黑蓝） | `#1a1d23`（炭灰） | 同站点跳转像换了产品 |
| 卡片 | `#1a1d27` | `#22262e` | 层级感不一致 |
| 主色 | `#58a6ff`（GitHub 蓝） | `#7aa2f7` / `#5e81ac`（Nord 蓝） | 同一个"保存"三种蓝 |
| 按钮 | 描边 + hover 填充 | 实心 `#3b4252` 或 `#5e81ac` | 主次操作无法区分 |
| 字体 | `-apple-system/Segoe UI` | `Microsoft YaHei` | 字重、行高全不同 |
| 图标 | emoji（🔥📊⚙️🔌💻📜） | 无图标 | 跨平台渲染不一致、无法着色 |

实测对比度：

| 组合 | 对比度 | 结论 |
|---|---|---|
| 现 muted `#6e7681` / bg `#0f1117` | **4.11:1** | 低于 AA 4.5:1 |
| 现按钮白字 `#fff` / accent `#58a6ff` | **2.53:1** | 明显不达标 |
| 现正文 `#c9d1d9` / bg | 12.23:1 | OK |

也就是说，登录按钮和次要说明文字在低亮度屏幕上已经是"看得见但费劲"。

### 1.3 结构性问题（按影响排序）

1. **无共享样式**：7 份插件页复制粘贴同一段 `body/nav/table/button` CSS，随后各自微调出不一致。
2. **导航割裂**：框架是 tab 切换（不换 URL），插件页是整页跳转（带 `?v=` 版本号）；同一站点两套导航心智。
3. **固定像素输入框**：`group.html` / `settings.html` 写死 `input{width:320px}`，`bili_live.html` 写死 `120px`。手机上 375px 宽直接横向溢出。
4. **数据展示原始**：仪表盘趋势用 `█` 字符拼柱状图（`index.html:341-359`），无法缩放、悬浮、看具体值；token 只有三个裸数字。
5. **反馈弱**：保存成功是一行彩字（`index.html:444`），插件页多数**不看 `response.ok`** 就显示"✅ 已保存"（见 UX U-14）。
6. **空状态缺失**：表格 `innerHTML=''` 后就是一片空白，首次进入/接口失败无法区分"没有数据"和"坏了"。
7. **日志不可用**：200 条环形缓冲、无过滤/搜索/暂停，心跳默认刷屏（UX U-10）。
8. **品牌分裂**：登录页和标题叫 "NapCatBot"（`index.html:6/146/158`），项目叫"大肥鱼"，还支持 official 通道（UX U-38）。
9. **第三方 CDN**：socket.io 从公网拉（`index.html:233`），内网环境实时日志直接失效（UX U-17）。

### 1.4 可访问性与工程性

- 无 `:focus-visible` 样式，键盘 Tab 到按钮看不出焦点；
- 模态框无 Esc 关闭、无焦点陷阱、无 `aria-modal`；
- emoji 导航无 `aria-label`，读屏读作"火焰"；
- 全站只有 `@media(max-width:768px)` 一个断点，只把网格改成单列，侧栏/表格未处理；
- 每次切 tab/定时器都全量 `fetch` 并 `innerHTML` 重绘（`index.html:287`），页面会闪。

---

## 2. 设计目标与原则

**目标**：把"能用的后台"做成"敢给群友看的后台"——统一、克制、信息密度高、手机上也能改配置。

四条原则：

1. **一个产品，一套皮肤**。框架与插件页共用 token 与外壳，用户感觉不到"跨系统"。
2. **颜色只表达状态**。主色只用于主操作与选中态；红/黄/绿只用于错误/警告/成功；装饰性用色一律去掉。
3. **先信息，后装饰**。KPI 数字要大且等宽，图表要有刻度与悬浮值；不为了好看牺牲可读性。
4. **渐进增强**。无 JS 时表单仍可提交、链接仍可点；样式失败时内容仍可读。

---

## 3. 设计体系（Design Tokens）

命名统一前缀 `--tds-`（Tide Design System），框架与插件页共用一份 `tokens.css`。

### 3.1 色彩（深色默认）

| Token | 值 | 用途 | 实测对比度 |
|---|---|---|---|
| `--tds-bg` | `#0B1018` | 页面底色（深海） | — |
| `--tds-surface` | `#121A28` | 卡片/面板 | — |
| `--tds-surface-2` | `#172133` | 卡片内分区、hover 行 | — |
| `--tds-surface-3` | `#1D2A40` | 选中项、激活 tab | — |
| `--tds-border` | `#24314A` | 常规描边 | — |
| `--tds-border-strong` | `#33455F` | 表头分隔、强调描边 | — |
| `--tds-text` | `#E9EFF8` | 主文本 | 16.49:1 ✅ |
| `--tds-text-2` | `#B3C0D3` | 次要文本 | 10.35:1 ✅ |
| `--tds-muted` | `#8494AB` | 说明/占位 | 6.18:1 ✅ |
| `--tds-accent` | `#45C4F5` | 主色（潮汐青） | 9.50:1 ✅ |
| `--tds-accent-hover` | `#6FD3FA` | hover | — |
| `--tds-accent-ink` | `#04141F` | **主色上的文字** | 9.30:1 ✅ |
| `--tds-success` | `#3FB950` | 成功/在线 | 7.50:1 ✅ |
| `--tds-warning` | `#E3B341` | 警告/重连中 | 9.80:1 ✅ |
| `--tds-danger` | `#F85149` | 危险/离线/删除 | 5.69:1 ✅ |

图表色板（按序取用，保证色盲可辨）：`#45C4F5`、`#3FB950`、`#E3B341`、`#BC8CFF`、`#FF7B72`、`#56D4B8`。

**关键规则**：主按钮用 `背景=accent` + `文字=accent-ink`（深色字），而不是现在的白字——白字在青蓝上只有 2.5:1。

语义映射：在线/启用 = success；重连/降级 = warning；离线/禁用/失败 = danger；普通链接 = accent。

### 3.2 字体与排版

```css
--tds-font-sans: -apple-system, "Segoe UI", "PingFang SC",
                 "Microsoft YaHei", system-ui, sans-serif;
--tds-font-mono: "JetBrains Mono", "Cascadia Code", Consolas, monospace;
```

| 级别 | 字号/行高 | 字重 | 用途 |
|---|---|---|---|
| display | 32/1.2 | 700 | 关键 KPI |
| h1 | 24/1.3 | 600 | 页面标题 |
| h2 | 20/1.35 | 600 | 区块标题 |
| h3 | 16/1.4 | 600 | 卡片标题 |
| body | 14/1.6 | 400 | 正文、表单 |
| body-sm | 13/1.55 | 400 | 表格、说明 |
| caption | 12/1.5 | 500 | 标签、时间戳 |

- 数字：`font-variant-numeric: tabular-nums`，KPI 与表格数值右对齐；
- 中文不回退到衬线；不引 web font（离线约束），仅用系统字体；
- 正文最小 12px，低于 12px 只允许用于 badge。

### 3.3 间距 / 圆角 / 阴影

- 间距：4 的倍数 `4/8/12/16/20/24/32/40`，拒绝 `13px`、`18px` 这类魔法值；
- 圆角：`sm 6`（输入框/按钮）· `md 10`（卡片）· `lg 14`（模态）· `pill 999`（徽章）；
- 描边：1px `--tds-border`，不用物理投影做层级；
- 阴影：`--tds-shadow-1: 0 1px 2px rgba(0,0,0,.30)`（卡片）、`--tds-shadow-2: 0 12px 32px rgba(0,0,0,.45)`（模态/Toast）；
- 层级 z-index 固定：`sticky 10 / dropdown 40 / modal 100 / toast 200`。

### 3.4 图标

- 弃用 emoji，改为 **24×24 线性 SVG**（`stroke-width:1.75`，`currentColor`），统一由 `icon(name)` 宏/JS 函数输出；
- 首批 25 个：dashboard、server、settings、plug、terminal、logs、users、brain、wrench、clock、sparkles、live、radio、search、filter、pause、play、download、refresh、check、x、chevron、trash、plus、external；
- 图标尺寸只有 `16 / 18 / 20` 三档；导航用 18，按钮用 16。

### 3.5 动效

- 时长：`120ms`（hover）· `180ms`（展开/折叠）· `240ms`（模态/抽屉）；
- 曲线：`cubic-bezier(.2,.7,.3,1)`；
- 只动 `opacity/transform`，不动 `width/height`；
- `@media (prefers-reduced-motion: reduce)` 全部降为 0。

---

## 4. 布局框架（App Shell）

### 4.1 桌面结构（≥1024px）

```
┌───────────────────┬──────────────────────────────────────────────────────────┐
│  ≈ Tide · 大肥鱼   │  仪表盘 / 群列表                     ● 已连接   ⟳ 重连   ⏻ │  ← 顶栏 56px，sticky
│                   ├──────────────────────────────────────────────────────────┤
│  总览              │  群 AI 概览                                              │
│   ▸ 仪表盘         │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐      │
│   ▸ 状态           │  │ 累计发送  │ │ 本分钟   │ │ 在线群数 │ │ 今日消耗 │      │  ← KPI 卡片
│                   │  │ 12,803   │ │ 14       │ │ 6 / 9    │ │ ¥1.24    │      │
│  管理              │  └──────────┘ └──────────┘ └──────────┘ └──────────┘      │
│   ▸ 系统配置       │  ┌───────────────────────────────┐ ┌────────────────────┐ │
│   ▸ 插件           │  │ 收发趋势（近 20 分钟）   SVG  │ │ 群列表              │ │
│                   │  │  ╱╲      ╱╲                   │ │ 群名 · 状态 · 活跃  │ │
│  观测              │  │ ╱  ╲____╱  ╲___               │ │ …                  │ │
│   ▸ 日志           │  └───────────────────────────────┘ └────────────────────┘ │
│                   │                                                          │
│  群 AI             │                                                          │
│   ▸ 群列表         │                                                          │
│   ▸ 全局设置       │                                                          │
│   ▸ 群友记忆       │                                                          │
│   ▸ MCP 工具       │                                                          │
│   ▸ 后台 / 轮询    │                                                          │
│   ▸ 提示词         │                                                          │
│   ▸ B 站直播       │                                                          │
│                   │                                                          │
│  v2.x  · 退出登录  │                                                          │
└───────────────────┴──────────────────────────────────────────────────────────┘
   侧栏 236px，固定                   内容区 max-width 1320px，页面 padding 24/32
```

要点：

- **侧栏**分组（总览 / 管理 / 观测 / 群 AI），组标题 12px muted 大写，选中项左侧 2px 青色指示条 + `surface-3` 底；
- **顶栏**只放全局操作：连接状态 pill（可点击展开详情）、重连、主题切换（M4）、登出；面包屑显示"分组 / 页面"；
- **内容区**统一 `.page` 内边距与 `.page-header`（标题 + 一句话说明 + 右侧主操作）；
- 框架原来的 5 个 tab 变成 5 个真实页面路由（或保留 tab 但 URL 用 `#/dash`），与插件页同级。

### 4.2 响应式三档

| 断点 | 侧栏 | 内容 | 表格 |
|---|---|---|---|
| ≥1024px | 236px 固定 | 双栏网格 | 标准表 |
| 768–1023px | 收起为 64px 图标栏，hover 展开浮层 | 单栏 | 横向滚动 |
| <768px | 顶部汉堡 + 抽屉；底部快捷栏（仪表盘/日志/群列表） | 单列，padding 16 | **行变卡片**（标签:值 两列） |

群设置/全局设置页在 <768px 时：分组导航收成下拉选择，保存栏吸底（`position:sticky; bottom:0`），输入框 `width:100%`。

### 4.3 与插件页的接入方式

插件页保持独立路由 `/deepseek/*`，但模板改为继承框架的 `base.html`（Flask 的 `DispatchingJinjaLoader` 会先查 app 模板、再查 blueprint 模板，因此插件模板可以直接 `{% extends "base.html" %}`，无需复制外壳）。

```jinja
{# plugins/deepseek/templates/settings.html #}
{% extends "base.html" %}
{% block title %}全局设置{% endblock %}
{% block breadcrumb %}<a href="/deepseek/dashboard">群 AI</a> / 全局设置{% endblock %}
{% block content %}
  <div class="page-header">…</div>
  …
{% endblock %}
{% block scripts %}<script src="/static/tds/app.js" defer></script>{% endblock %}
```

侧栏的"群 AI"分组仅在插件加载成功后渲染（由 base 模板读取 `plugin_manager`），插件被禁用时不该出现死链接。

---

## 5. 组件规范

统一前缀 `.tds-*`，组件样式只依赖 token，页面不允许再写颜色字面量。

| 组件 | 类名 | 规格要点 |
|---|---|---|
| 按钮 | `.tds-btn` + `--primary/--secondary/--danger/--ghost` | 高 36（默认）/30（sm）/42（lg）；主按钮青底深字；危险按钮红描边，hover 才填充；loading 态内置 spinner 并禁用 |
| 输入框 | `.tds-input` | 高 36，聚焦 2px `accent` 环；错误态红描边 + 下方红字 12px；支持前缀图标与后缀单位 |
| 表单字段 | `.tds-field` | label 12px muted 上置；说明 12px muted；错误 12px danger；必填用 `*` |
| 开关 | `.tds-switch` | 32×18 轨道，选中 `accent`；替代裸 checkbox（配置项大量使用） |
| 选择器 | `.tds-select` | 原生 select 美化，箭头用 SVG mask；不用自定义下拉（无障碍成本高） |
| 分组区块 | `.tds-section` | 替代 fieldset/legend：标题 16px + 说明 13px + 内容卡，间距 24 |
| 卡片 | `.tds-card` | surface 底 + 1px 描边 + 10px 圆角；标题栏可选右侧操作 |
| KPI | `.tds-kpi` | 标签 12px muted、数值 28px 等宽、副文案 12px；支持趋势箭头与颜色 |
| 徽章 | `.tds-badge` | 6 种语义色；`--dot` 变体带 6px 圆点（在线/离线状态） |
| 表格 | `.tds-table` | 行高 44，表头 12px muted 大写；hover 行 `surface-2`；数值列右对齐；行操作 hover 显现；<768px 自动转卡片 |
| 分页 | `.tds-pager` | 上一页/页码/下一页 + 每页条数；与 URL query 同步 |
| 空状态 | `.tds-empty` | 图标 + 一句话 + 主操作按钮（如"去开启一个群"） |
| 骨架屏 | `.tds-skeleton` | 卡片/表格两种形状；数据 >200ms 才出现，避免闪烁 |
| Toast | `.tds-toast` | 右上角，成功/失败/信息三种；3s 自动消失，最多堆叠 3 条；失败带"详情"展开 |
| 模态 | `.tds-modal` | 遮罩 `rgba(4,8,14,.72)` + 背景模糊 4px；Esc 关闭、焦点陷阱、`aria-modal`；危险确认按钮红底 |
| 抽屉 | `.tds-drawer` | 右侧 420px，用于记忆详情、运行详情；<768px 全屏 |
| Tabs | `.tds-tabs` | 下划线式，选中 2px accent；用于群设置分区 |
| 日志台 | `.tds-terminal` | 等宽 12px、行高 1.6；级别色点 + 来源 chip；工具栏含级别/来源过滤、搜索、暂停、清空、导出 |
| 图表 | `.tds-chart` | 纯 SVG：折线/面积/柱状/迷你 sparkline；1px 网格，2px 线，悬浮十字线 + 数值提示 |
| 代码块 | `.tds-code` | 深底 mono，行号可选，右上角复制按钮；MCP/自定义工具用 |

**交互一致性规则**：

1. 所有会写数据的按钮，点击后立即进入 `loading`，成功弹 Toast、失败弹 Toast + 保留表单内容；
2. 所有危险操作（清空记忆、删除记忆、停用插件、`split_max_len=0`）必须二次确认，确认框写清"影响范围"；
3. 所有列表页必须实现"加载中 / 空 / 出错 / 有数据"四种状态；
4. 所有表单保存按钮吸底，显示"有 N 项未保存"计数，离开页面时提示。

---

## 6. 页面级改版

### 6.1 登录页

- 品牌统一为 **"Tide · 大肥鱼"** + 一句副标题"QQ 群 AI 管理台"（解决 U-38）；
- 输入框加"显示/隐藏密码"；错误区分"密码错误"与"面板未配置密码"（配合 UX U-05）；
- 底部加一行状态提示："面板可独立于 QQ 连接访问"，并显示版本号；
- 背景用极淡的潮汐渐变网格（CSS 绘制，不用图片），保持克制。

### 6.2 仪表盘（框架首页）

- 6 张平铺卡片 → **4 张 KPI + 2 张状态卡**：累计收发、本分钟收发、WS 状态、发送成功率（新增，取 `/api/monitor` 可扩展字段，无字段时显示"—"）；
- 趋势图：`█` 字符 → SVG 面积图（收发双线），X 轴 20 分钟刻度，悬浮显示"12:34 收 12 / 发 3"；
- 卡片点击跳到对应页（发送数 → 日志页并预置过滤）。

### 6.3 系统配置

- 现为单列长表单（19 个字段），改为 **左侧分组导航 + 右侧卡片**：基础 / Web 面板 / 官方机器人；
- 每项加一行说明（现在 label 里塞长句）；
- 危险项（`web_admin_host` 填 `0.0.0.0`）给黄色提示；
- 保存按钮吸底，显示未保存计数；保存失败定位到具体字段。

### 6.4 插件列表

- 表格 → 卡片列表：图标 + 名称/版本 + 状态徽章 + 描述（失败时展开红色错误详情，解决 U-11 的可见性）；
- 操作按钮收进 `⋯` 菜单（页面 / 重载 / 禁用 / 配置），危险项红色；
- 顶部加搜索框与"仅显示已启用"过滤。

### 6.5 状态

- 4 张卡改为"资源仪表"：环形进度（SVG）+ 绝对值 + 明细（核心数、总内存）；
- 1s 轮询时数字做平滑过渡（CSS transition），不整块重绘。

### 6.6 日志（重点）

- 工具栏：级别多选（info/warn/error）、来源下拉、关键字搜索（高亮命中）、暂停/继续、清空、导出 `.log`；
- 心跳行默认折叠/降噪（跟随 `show_heartbeat`，并单独给"显示心跳"开关）；
- 行样式：时间 12px 等宽 muted、来源 chip、正文；错误行左侧 2px 红条；
- 支持"跟随最新"和"跳到底部"；暂停时显示"N 条新日志"浮标；
- 切走再回来保留过滤条件（localStorage）。

### 6.7 群列表（deepseek dashboard）

- 表头改：群名（需后端补 `group_name` 落库，见 UX U-13，属跨端依赖，可先显示"群 123456"占位）· 状态 · 最近活跃 · 今日 token · 操作；
- token 三数字 → 迷你条形（命中/未命中/输出三段）+ 悬浮明细与命中率；
- 行操作：开关（switch）、设置、清空记忆（危险，进 `⋯`）；
- 顶部：搜索群号/群名、"只看已开启"、批量开关（可选）。

### 6.8 群设置（group.html）

- 6 个 fieldset → **左侧分区导航**（人设 / 群级覆盖 / 风格 / 约定 / 工具 / 心情）+ 右侧卡片；
- 顶部标题显示"群名 + 群号 + 状态"；
- "群级覆盖"的每项显示"继承全局：xxx"的当前生效值（灰字），避免"留空到底等于什么"的困惑；
- 保存吸底；工具勾选用可搜索的多选列表，替代一整片 checkbox。

### 6.9 全局设置（settings.html）

- 24 个 fieldset → 左侧 6 组（模型 / 交互 / 记忆 / 自动行为 / 集成 / 多模态），组内锚点；
- 顶部搜索框实时过滤字段（解决 U-30）；
- 每个 fieldset 变成 `.tds-section`，默认只展开标题，可"全部展开"；
- 危险值（`split_max_len<=0`、`speakup` 频率过高）就地红/黄提示；
- 页面底部固定"保存全部 / 重置"栏。

### 6.10 群友记忆（memory.html）

- 两张表 → tabs（群友记忆 / 自我记忆）；
- 群友记忆：搜索（昵称/QQ/内容）、按重要性排序、标签 chip、分页（每页 20）、批量删除；
- 点行开右侧抽屉：完整内容、来源消息、创建/更新时间、编辑/删除（解决 U-33）；
- 顶部统计条：总条数、今日新增、覆盖群友数。

### 6.11 MCP 工具（mcp.html）

- JSON textarea → **表单化**：类型选择（stdio / http）+ 对应字段（command/args/env 或 url/headers），"高级 JSON"折叠保留（解决 U-34）；
- 运行状态用徽章（running/failed/stopped）+ 最后错误摘要；
- 连接/断开按钮带 loading 与结果 Toast。

### 6.12 后台 / 轮询（runs.html）

- 三段（运行记录 / 轮询 / 监测）→ tabs；
- 运行记录：状态徽章 + 目标 + 开始时间 + 耗时 + 摘要；点行开抽屉看 trace；
- 文件/进程状态用等宽小表格；刷新按钮 + 自动刷新开关；
- 长文本（任务描述）截断两行 + "展开"。

### 6.13 提示词（prompts.html）

- 卡片列表显示"默认 / 已覆盖"徽章 + 字数 + 更新时间；覆盖项左边 2px 绿条；
- 编辑用全宽 textarea + 字数统计 + "恢复默认"（危险二次确认）；
- 支持按分类折叠与搜索。

### 6.14 B 站直播（bili_live.html）

- 顶部状态 Hero：直播中/离线（大状态徽章）+ 房间号 + 观看人数 + 开始时间；
- 凭据状态卡：已登录/未登录/缺依赖，缺 `bilibili_api` 时直接给安装命令（配合审计 M-03）；
- 配置按"接入 / 身份绑定 / TTS / 问答"分组为 tabs；
- 长表单吸底保存。

### 6.15 观察页（live/danmaku、live/debug）

- `danmaku.html` 是 OBS 透明浮层，**只做三件事**：字体加 1px 深色描边提升可读性、弹幕进入/离开缓动、低透明度背景可调；不引入外壳与导航；
- `debug.html` 是调试工具页，改用 `.tds-terminal` 风格 + 过滤 chip + 吸顶工具栏，保留全部现有功能与快捷键。

---

## 7. 技术实现

### 7.1 目录结构（新增，不改现有逻辑）

```
ui/web/
├── static/                       # 新增：由 server.py 的 static_folder 指向这里
│   └── tds/
│       ├── tokens.css            # 颜色/字号/间距/圆角/阴影（~4 KB）
│       ├── base.css              # reset、布局、排版、外壳（~8 KB）
│       ├── components.css        # 20 个组件（~14 KB）
│       ├── icons.js              # SVG 图标表 + icon()（~6 KB）
│       └── app.js                # toast/modal/tabs/chart/log 工具（~12 KB）
├── templates/
│   ├── base.html                 # 新增：App Shell
│   ├── _macros.html              # 新增：icon()/badge()/field()/empty()
│   └── index.html                # 改为 extends base.html
plugins/deepseek/templates/
└── *.html                        # 逐个改为 extends "base.html"
```

`ui/web/server.py` 现在把 `static_folder` 指向了 `templates/`（`server.py:19-23`），改为指向新 `static/` 目录即可；插件页与框架页同源，统一引用 `/static/tds/*.css`。

### 7.2 外壳模板骨架

```jinja
{# ui/web/templates/base.html #}
<!doctype html>
<html lang="zh-CN" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{% block title %}Tide{% endblock %} · 大肥鱼</title>
  <link rel="stylesheet" href="/static/tds/tokens.css?v={{ TDS_VERSION }}">
  <link rel="stylesheet" href="/static/tds/base.css?v={{ TDS_VERSION }}">
  <link rel="stylesheet" href="/static/tds/components.css?v={{ TDS_VERSION }}">
</head>
<body class="tds">
  <aside class="tds-sidebar">{% include "_nav.html" %}</aside>
  <div class="tds-main">
    <header class="tds-topbar">
      <div class="tds-breadcrumb">{% block breadcrumb %}{% endblock %}</div>
      <div class="tds-topbar-actions">…连接状态 / 重连 / 登出…</div>
    </header>
    <main class="tds-content">{% block content %}{% endblock %}</main>
  </div>
  <script src="/static/tds/icons.js" defer></script>
  <script src="/static/tds/app.js" defer></script>
  {% block scripts %}{% endblock %}
</body>
</html>
```

### 7.3 兼容与迁移策略

1. **变量别名层**：`tokens.css` 里保留旧变量名映射，`--bg: var(--tds-bg)` 等，未迁移页面自动跟随新色板，不会花屏；
2. **一页一迁**：先迁 `index.html`（外壳 + 5 页），再逐页迁插件页；未迁移的插件页继续用自带 `<style>`，只是顶栏由 base 提供；
3. **API 不变**：只改前端渲染与请求封装，`/api/*`、`/deepseek/api/*` 的路径、入参、返回完全不动；
4. **统一请求封装**：`api(path, opts)` 统一处理 401（清 token 回登录页，解决 U-16）、`response.ok` 检查、错误 Toast；
5. **无构建**：CSS 手写、JS 用原生 ES2019，`defer` 加载；图标用 JS 对象 + `innerHTML` 注入（同源静态资源，安全）；
6. **版本化**：静态资源 `?v={{ TDS_VERSION }}`，与插件页现有 `?v={{ PLUGIN_VERSION }}` 风格一致，避免缓存旧样式；
7. **socket.io 本地化**：把 `socket.io.min.js` 放进 `static/vendor/`，CDN 失败不再影响日志（解决 U-17）。

**实施补记（2026-11-26）：** 实际落地时静态根定为 `ui/web/static/`（`server.py` 显式 `static_url_path="/static"`），样式分三层 `tokens.css`（变量）→ `app.css`（外壳+组件）→ `pages.css`（迁移后页面共用：字段组/表单/表格/代码块 + 各页原组件类，全部限定在 `.tds-content`）。7 个插件页的 `<style>` 已全部删除；`legacy.css` 只剩未迁移的旧页面（审批中心、观察页）在用。

### 7.4 性能预算

| 指标 | 预算 |
|---|---|
| CSS 总量（gzip） | ≤ 26 KB |
| JS 总量（gzip） | ≤ 30 KB |
| 首屏请求 | ≤ 8 个（HTML/CSS×3/JS×2/字体 0/图标内联） |
| LCP（局域网） | ≤ 1.5 s |
| 轮询重绘 | 只更新变化节点，不整块 `innerHTML` |
| 外部依赖 | 0（无 CDN、无 web font） |

---

## 8. 实施分期

| 里程碑 | 范围 | 交付物 | 预估 |
|---|---|---|---|
| **M0 基建** | token、base 外壳、宏、登录页、静态目录、`api()` 封装、兼容别名 | 可运行的框架外壳；`index.html` 换成新壳，5 个 tab 内容暂沿用旧样式 | 2.5 人日 |
| **M1 框架 5 页** | 仪表盘（含 SVG 趋势）、配置、插件、状态、日志 | 框架面板全量新 UI，日志过滤/搜索/暂停上线 | 3 人日 |
| **M2 插件页迁移** | dashboard、group、settings、memory、mcp、runs、prompts、bili_live | 8 页统一外壳，删除各自 `<style>` 与重复 nav | 8.5 人日 |
| **M3 数据与工作台** | 图表组件、token 分段条、表格卡片化、移动端卡片布局 | 数据可视化与移动端表格 | 1.5 人日 |
| **M4 打磨** | 浅色主题（可选）、键盘/读屏、空/错/骨架态补齐、性能回归、截图基线 | 验收报告 + 视觉基线截图 | 1.5 人日 |

合计 **17 人日（±20%）**。每个里程碑独立可上线，M0 完成即可回滚单页，不阻塞后续修复。

**依赖与前置**：

- 群名显示依赖后端 `groups.group_name` 落库（UX U-13），可先占位；
- 发送成功率、失败数依赖 `/api/monitor` 增加字段（可选，缺省显示"—"）；
- 浅色主题依赖 token 层完成，放 M4 且可不做。

---

## 9. 验收标准

**视觉**

- [ ] 全站无颜色字面量（除 token 定义）；改 `tokens.css` 一处，11 个页面同步变化；
- [ ] 同一语义只有一种颜色：主操作=青、成功=绿、警告=黄、危险=红；
- [ ] 按钮、输入框、卡片在 11 个页面上圆角/高度/间距一致；
- [ ] 无 emoji 当图标（OBS 弹幕页除外）。

**交互**

- [ ] 所有列表页具备"加载中/空/出错/有数据"四态；
- [ ] 所有写操作有 loading、成功/失败 Toast；失败不显示"已保存"；
- [ ] 危险操作有二次确认，文案写清影响范围；
- [ ] 401 自动回登录页，不出现空白页；
- [ ] 表单离开时提示未保存。

**可访问性**

- [ ] 正文对比度 ≥ 4.5:1，大字 ≥ 3:1（重点：主按钮文字）；
- [ ] 键盘可完成"登录 → 切页 → 改配置 → 保存"全流程，焦点环可见；
- [ ] 模态 Esc 可关、焦点不逃逸；
- [ ] 图标按钮有 `aria-label`。

**响应式**

- [ ] 375px 宽无横向滚动，表格变卡片，配置页可保存；
- [ ] 768–1023px 侧栏收起不遮挡内容。

**工程**

- [ ] 无新增外部网络依赖，断网可用；
- [ ] 首屏 CSS+JS ≤ 56 KB（gzip）；
- [ ] `python -m pytest tests/ -q` 仍为 78 passed；
- [ ] 旧 URL 全部可访问，API 无破坏性变更。

---

## 10. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 插件页模板各自带 `<style>`，迁移期新旧样式互相污染 | 中 | M0 的变量别名层 + `.tds` 作用域；未迁移页面不加 `.tds` 类 |
| Flask 模板加载器顺序（app 先于 blueprint） | 低 | 已按 Flask `DispatchingJinjaLoader` 行为设计，M0 先用一页验证 |
| 插件重连时重复注册 Blueprint（审计 M-18） | 中 | 本方案不改注册逻辑；外壳导航改为每次请求动态渲染，不闭包实例 |
| 群名/成功率等字段后端没有 | 低 | 先占位"群 123456"/"—"，后端就绪后自动显示 |
| 改动面大导致回归 | 中 | 一页一迁、按里程碑合并；每期跑 pytest + 手工走查清单 |
| 「美化」被当成「顺手改行为」 | 中 | 行为类问题（U-01～U-09 等）不在本方案内，另立任务 |

---

## 附录 A：页面 → 文件 → 工作量

| 页面 | 文件 | 现状 | 目标要点 | 人日 |
|---|---|---|---|---|
| 外壳/登录 | `base.html` `_macros.html` `_nav.html` | 无 | 侧栏 + 顶栏 + 品牌 | 2.5 |
| 框架 5 tab | `ui/web/templates/index.html` | 639 行 | 4 页迁移 + 日志台 | 3.0 |
| 群列表 | `dashboard.html` | 68 行 | 群名/token 条/搜索 | 1.0 |
| 群设置 | `group.html` | 353 行 | 分区导航 + 继承值 | 1.5 |
| 全局设置 | `settings.html` | 435 行 | 6 组 + 搜索 + 折叠 | 2.0 |
| 记忆 | `memory.html` | 162 行 | tabs + 分页 + 抽屉 | 1.0 |
| MCP | `mcp.html` | 100 行 | 表单化 + 状态 | 0.5 |
| 后台/轮询 | `runs.html` | 355 行 | tabs + 抽屉 | 1.0 |
| 提示词 | `prompts.html` | 162 行 | 卡片 + 覆盖标记 | 0.5 |
| B 站直播 | `bili_live.html` | 457 行 | 状态 Hero + 分组 | 1.0 |
| 观察页 | `live/*.html` | 2 页 | 微调 | 0.5 |
| 组件/图表/回归 | `tds/*` | — | 组件库 + 验收 | 2.5 |

## 附录 B：与体验评估的对应关系

| UX 编号 | 本方案是否解决 | 说明 |
|---|---|---|
| U-10 日志无过滤 | ✅ 完全 | 日志台工具栏 |
| U-11 插件错误不可见 | ✅ 完全 | 卡片展开错误 |
| U-13 群列表只有群号 | ◐ 部分 | 前端已留位，需后端落库群名 |
| U-14 保存无真实反馈 | ✅ 完全 | Toast + `response.ok` + loading |
| U-16 会话过期白屏 | ✅ 完全 | `api()` 统一 401 |
| U-17 Socket.IO CDN | ✅ 完全 | 本地 vendor |
| U-30 设置页无导航 | ✅ 完全 | 分组 + 搜索 |
| U-31 模型 ID 手填 | ◐ 部分 | 提供示例与格式校验；"拉取模型"需后端接口 |
| U-32 token 裸数字 | ✅ 完全 | 分段条 + 命中率 |
| U-33 记忆无分页 | ✅ 完全 | 分页 + 抽屉 |
| U-34 MCP 手写 JSON | ✅ 完全 | 表单化 + 高级 JSON |
| U-38 品牌不一致 | ✅ 完全 | 统一 "Tide · 大肥鱼" |
| U-01/U-02/U-03/U-05 等 | ❌ 不在范围 | 属启动流程/默认值，需单独改代码 |

## 附录 C：设计基线截图（后续补充）

M4 完成时在 `docs/screenshots/` 下补 6 张基线图：登录、仪表盘、日志、群设置（桌面）、群设置（375px）、群列表。本文档与 `docs/webui-mockup-2026-11-25.html` 作为设计基线，实现与基线的偏差需在 PR 中说明。

---

*本方案为设计文档，未修改任何代码。文中行号基于 2026-11-25 的工作区（HEAD `164262e`）。*
