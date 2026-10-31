# 看B站视频 + 发评论（全 API 流程，不走 DOM 点击）

> 血泪教训：B 站评论区是**多层 shadow DOM**（bili-comments → shadow → bili-comments-header-renderer → shadow → bili-comment-box → shadow → bili-comment-rich-textarea → shadow → #input），
> Shadow DOM 里的输入框 offsetHeight 经常是 0，Playwright 的 click / insertText 会报 "no element"，
> 而且页面会自动跳到推荐视频（BV 变了）、评论列表 shadowRoot.textContent 基本取不到正文。
> **结论：发评论、读评论、读字幕一律用页面内的 fetch + cookie，稳、快、不用点。**

前提：chrome 里已登录 B 站（账号 泉此方QuanC1fang / mid 1776078293）。用 playwright 打开任意 bilibili 页面即可，`page.evaluate` 里的 fetch 自带 cookie。

## 0. 拿 csrf
```js
const csrf = document.cookie.split('; ').find(x=>x.startsWith('bili_jct='))?.split('=').slice(1).join('=');
```

## 1. 视频信息 + aid/cid
```
GET https://api.bilibili.com/x/web-interface/view?bvid={BVID}
→ data.aid, data.cid, data.title, data.owner.name, data.desc, data.duration, data.stat
```

## 2. 拿字幕当“看过”的依据（关键！）
```
GET https://api.bilibili.com/x/player/v2?aid={aid}&cid={cid}
→ data.subtitle.subtitles[]  （通常 lan='ai-zh'，字段名可能是 subtitle_url 或 url，以 // 开头要补 https:）
再 GET 那个 json → body[].content 拼起来就是全文
```
有字幕就读字幕写真实观后感；没字幕就退而用 `data.desc` + 热门评论内容推断，**别硬编细节**。

## 3. 读评论（写“接梗”式评论前必读）
```
GET https://api.bilibili.com/x/v2/reply/main?type=1&oid={aid}&mode=2&next=0   // mode=2 最新
```
→ data.replies[]：member.uname、content.message、rpid、ctime。
排序用 `sort=2` 那个老接口 /x/v2/reply 会返回 -400，**用 reply/main**。
评论正文在 API 里有，不用去爬 DOM。

## 4. 发主评论（@某人）
```js
await fetch('https://api.bilibili.com/x/v2/reply/add', {
  method:'POST', credentials:'include',
  headers:{'Content-Type':'application/x-www-form-urlencoded'},
  body: new URLSearchParams({
    oid: aid, type:'1',
    message: '正文…… @隔壁逍遥呐',
    csrf
  })
});
// code===0 成功，data.rpid 是新评论 id
```
@ 生效的验证：`reply/main` 里自己那条 `content.members[0].uname === '隔壁逍遥呐'` 就说明 @ 成功被解析了。

## 5. 回复别人的评论（挑一两条互动）
```js
body: new URLSearchParams({ oid, type:'1', message, csrf, root: 目标rpid, parent: 目标rpid })
```
是**回复楼中楼**时 root 和 parent 都填目标评论的 rpid。
验证：`GET /x/v2/reply/reply?type=1&oid={aid}&root={rpid}&ps=10&pn=1` → data.replies 里能看到自己。

## 6. 发完自查
- reply/main mode=2 第一条附近找 `泉此方QuanC1fang`，确认正文在、没被折叠。
- code: -412 限流 / -111  csrf 失败 / -400 参数错（多半 oid 用了 BV 没换 aid）。

## 7. 定时刷 B 站的标准动作（每档任务按这个走）
1. 选视频：从首页推荐 / 关注动态里挑，避开纯广告、擦边、时政吵架。
2. 打开 `https://www.bilibili.com/video/{BV}`，`page.evaluate` 里 `document.querySelector('video')?.pause()` 防止自动跳下一个 BV。
3. 按上面 §1 §2 读 aid/cid/字幕，30 秒内搞清视频讲了什么。
4. §3 拉最新评论，挑 1~2 条能接的。
5. §4 发主评论（含 @隔壁逍遥呐 + 标题要点 + 真实感受），§5 回一条楼中楼。
6. 群里只发一行记账：`✅ BVxxxx 已评 @你｜标题｜一句感想`。
7. 全程别截图、别点 DOM，能省一半工具调用。

## 8. 已知坑
- 页面会自动跳 BV：每轮操作前重新 `page.goto` 目标 BV，动作都在同一个 evaluate 里做完。
- `bili-comments` 的 shadowRoot 里**有一个 0x0 的幽灵 bili-comment-box**，真正的在 `bili-comments-header-renderer` 的 shadowRoot 里。#input 在 `bili-comment-rich-textarea` 的 shadowRoot 里。
- 评论列表 `#feed` 里的 bili-comment-renderer shadowRoot 取 textContent 只有"置顶"，正文还是走 API。
- 一次 `page.evaluate` 里可以连做好几个 fetch，省调用次数。

## 6. 定时任务无人值守版（一次 evaluate 跑完全流程）

定时任务触发时没人盯着，禁止点 DOM、禁止等页面加载，全部走 fetch。把下面这段塞进一次 `browser_evaluate`（或 playwright `browser_run_code_unsafe`），改掉 BVID 就行：

```js
async () => {
  const csrf = document.cookie.split('; ').map(s=>s.trim()).find(x=>x.startsWith('bili_jct='))?.split('=').slice(1).join('=');
  const BVID = 'BV1xxxxxxxxx';
  const j = async u => (await fetch(u, {credentials:'include'})).json();

  // 1) aid / cid / 标题
  const v = await j('https://api.bilibili.com/x/web-interface/view?bvid=' + BVID);
  const { aid, cid, title, desc } = v.data;

  // 2) 字幕（有就读，没有就用 desc）
  const p = await j(`https://api.bilibili.com/x/player/v2?aid=${aid}&cid=${cid}`);
  const sub = (p.data.subtitle?.subtitles || [])[0];
  let text = desc || '';
  if (sub) {
    const u = (sub.subtitle_url || sub.url).replace(/^\/\//, 'https://');
    const s = await (await fetch(u)).json();
    text = s.body.map(x => x.content).join('\n');
  }

  // 3) 最新评论（用来接梗）
  const c = await j(`https://api.bilibili.com/x/v2/reply/main?type=1&oid=${aid}&mode=2&next=0`);
  const hot = (c.data.replies || []).slice(0, 8).map(r => r.member.uname + '：' + r.content.message);

  // 4) 发评论（必须 @隔壁逍遥呐）
  const message = '……正文（口语、像真人、别提AI）…… @隔壁逍遥呐';
  const r = await fetch('https://api.bilibili.com/x/v2/reply/add', {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: new URLSearchParams({ oid: aid, type: '1', message, csrf })
  });
  const res = await r.json();
  if (res.code !== 0) return { ok: false, step: 'add', res };

  // 5) 立刻核验 @ 有没有被解析成蓝链
  const back = await j(`https://api.bilibili.com/x/v2/reply/main?type=1&oid=${aid}&mode=2&next=0`);
  const mine = (back.data.replies || []).find(x => x.rpid === res.data.rpid);
  const atOk = !!mine?.content?.members?.some(m => m.uname === '隔壁逍遥呐');
  return { ok: atOk, rpid: res.data.rpid, title, atMembers: mine?.content?.members, hot };
};
```

失败处理：`code===0` 但 `atOk===false` 就是 @ 没生效（账号昵称改了/对方改了名），立刻在原评论下补一条带 @ 的回复，仍然不行就群里说明「这次 @ 没解析出来」。

## 7. 无人值守注意事项

- 先 `browser_tabs list`，没有 bilibili 页面就 `browser_navigate` 到 `https://www.bilibili.com/`（不要导航到具体视频，热门页会被自动跳转打乱）。
- 选片：`GET https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1` 一次拿 20 条（bvid/title/owner/stat/dur/desc/tname），按"时长 8~25 分钟、播放高、标题不像标题党"挑。同一 BV 不要重复发（先查群里/数据库最近发过的 BV）。
- 发之前必读最新评论，评论里已经在刷的梗可以接，但**别复制别人原话**。
- 一天多次的档要换片子换角度：上午偏资讯/科普，中午偏下饭搞笑，晚上偏长视频/纪录片。
- 全部走完再回群，一条消息说清楚：发了哪个 BV、标题、评论 id、@ 是否解析成功。
- 遇到 `-101`（未登录）就是浏览器登录态掉了，别重试，直接群里喊人来重新登录。
- 遇到 `-412`（风控）停手等 20 分钟，不要连续刷请求。

## 6. 核验踩坑（2026-10-25 实测补充）

**坑 A：`/x/v2/reply/detail` 对某些 rpid 返回 -400**
不是没发成功，是老接口对部分评论（尤其楼中楼子回复）取不到。**改用 `/x/v2/reply/reply?type=1&oid={aid}&root={root_rpid}&ps=20&pn=1..N` 翻子楼**，按 rpid 精确找自己的那条，`ps=20` 一页，翻到 `list.length < 20` 为止。

**坑 B：只有 @ 没有正文的主评论，`reply/main` 的最新页抓不到**
`content.message` 为空、只有 `content.members` 的评论（比如只写了「@隔壁逍遥呐」），在 `reply/main?mode=2` 里可能根本不出现。
**这不代表没发成功。** 判据改成：翻根评论子楼（`reply/reply`）第 1~4 页，能找到自己的 rpid 和 `content.members` 就算成功。
所以核验顺序固定为：
1. `reply/main?mode=2` 前 3 页找主评论；
2. 找不到 → `reply/reply` 按对应 root 翻 1~4 页找子回复/@ 评论；
3. 都没有才判定失败（可能真被吞了，重试一次）。

**坑 C：`content.members` 才是 @ 成功与否的唯一证据**
正文里带不带「@某某」字样都不算数，只有 `members` 数组里出现目标 uname，才说明 B 站解析成蓝链了。每次核验必须把 `members` 打出来对一遍。

**踩坑速查表**：一次跑完 7 条评论，最终 7 条全落地，其中 4 条靠子楼核验才确认（`reply/main` 只捞到 3 条）。

## 7. 发评论一律 @ 主人（隔壁逍遥呐）

主人要求：**每次发主评论/推荐评论都要 @ 隔壁逍遥呐**。
写法：正文末尾加 ` @隔壁逍遥呐`（前面留一个空格），发完在 `members` 里确认解析成功。
回复别人的评论（楼中楼）不强制 @ 主人，除非内容本身在跟主人说话。

## 8. 绝对不要重复回复同一条评论（去重铁律）

发楼中楼之前**必须先查这条 root 底下我是不是已经回过**，回过就跳过，绝不再发。

去重判定：`/x/v2/reply/reply?type=1&oid={aid}&root={目标rpid}&ps=20&pn=1..N` 翻完，
`list.some(r => r.mid === 1776078293)` 为真 → 已经回过，直接跳过。

```js
async function alreadyReplied(aid, rootRpid) {
  const MY_MID = 1776078293;
  for (let pn = 1; pn <= 5; pn++) {
    const r = await (await fetch(`https://api.bilibili.com/x/v2/reply/reply?type=1&oid=${aid}&root=${rootRpid}&ps=20&pn=${pn}`, {credentials:'include'})).json();
    const list = r?.data?.replies || [];
    if (list.some(x => x.mid === MY_MID)) return true;
    if (list.length < 20) break;
  }
  return false;
}
```

主评论同理：发之前先把 `reply/main?mode=2` 和子楼翻一遍，
**同一条视频我发过主评论了就不要再发第二条**（token 也拦，但别指望它）。

判定脚本结构固定成这样，一轮里所有候选评论先过滤一遍再发：

```js
const picked = [];
for (const c of candidates) {
  if (c.mid === MY_MID) continue;                    // 自己的不回
  if (await alreadyReplied(aid, c.rpid)) continue;   // 回过的跳过
  picked.push(c);
  if (picked.length >= 2) break;
}
```

**为什么必须查**：B 站不会拦重复回复，同一句能发两遍；reply/main 有收录延迟，
光看最新页判断"没回过"会误判，所以只能靠 root 子楼逐页翻。
