---
name: find-skills
description: 查找尚未安装的第三方技能。提取关键词，先扫本地已装技能去重（避免重复装），再搜多源（GitHub API/awesome 精选列表/web_search），按人气排序推荐 top 5：安装量/star ≥1000 优先推荐、<100 明确标注"早期项目慎用"、无维护降权，验证真技能后给出可直接执行的安装指令（复制目录或 zip 导入技能市场）。用于"find a skill / 有什么技能能做 X / 找个技能 / 装个技能 / skill for X"。
version: 1.0.0
category: dev-workflow
author: QunWork (蒸馏自 oleksbard/claude-find-skill, MIT)
tags: skill-discovery, search, third-party, install
---

# 查找第三方技能（find-skills）

## 何时使用

用户想找**还没装**的技能——"有没有技能能做 X""找个技能""装个技能""skill for X"。**只发现与推荐，不自动安装**（安装需用户确认）。创建/优化技能用 skill-creator；从已有技能里选用直接查本机目录。

## 流程

### 1. 提取关键词
从用户需求提取 2-4 个搜索关键词（主题 + 动词 + 关键名词）。需求太模糊（如"找个有用的技能"）→ 先 ask_user 问一个澄清问题。

### 2. 列出已安装技能（去重，避免重复装）
扫全部技能层级，建"已装"集合：
- 内置层：`<安装目录>/coworker/skills/`
- 用户级：`%APPDATA%\coworker\skills\`（Windows）或 `~/.coworker/skills`（若存在）
- 工作区级：`<工作区>/.coworker/skills/`

**任何候选命中已装集合必须排除**，或标注"你已经有 `<name>` 了"——绝不当作新技能推荐。这是本技能的核心价值：避免重复装。

### 3. 多源搜索（三路独立，一路失败继续其余）

**A. GitHub 仓库搜索（主来源，人气门槛由服务端施加）**
用 `web_fetch` 调 GitHub 搜索 API（无认证限制 ~10 次/分，够用）：
```
https://api.github.com/search/repositories?q=<关键词> skill SKILL.md stars:>=100&sort=stars&per_page=20
```
读返回的 items：full_name、stargazers_count、pushed_at、html_url、description。
- **人气信号 = stars**（第三方技能流行度的最可靠代理）。
- **新鲜度信号 = pushed_at**（最近推送月份，不是 updated_at——后者点个星也会变）。

**B. awesome 精选列表（人工筛选，信号好）**
`web_fetch` 抓 `github.com/hesreallyhim/awesome-claude-code`（及同类 curated 列表），匹配关键词条目。推荐前验证链接有效。

**C. web_search 兜底**
`web_search "claude skill <主题> SKILL.md"` 或 `"agent skill <主题>"`，再 `web_fetch` 最有希望的仓库确认其真有 SKILL.md。

### 4. 合并、去重、排名（按人气门槛）

- **跨源去重**：按仓库 URL/技能名去重；命中已装的排除或标注"你已有"。
- **人气门槛（用户约定）**：
  - **≥1000 stars/installs → 优先推荐**，标注"高人气"
  - **100-999 → 正常推荐**
  - **<100 → 明确标注"早期项目，慎用"**——可能是单作者试水或未经验证
  - 无维护信号（pushed_at 距今 >6 个月）→ 降权或标注"维护停滞"
- **验证真技能**：GitHub 候选在进 top 5 前必须确认仓库里有 SKILL.md——`web_fetch` 仓库文件列表检查（或 curl raw.githubusercontent.com 试探）。确认不了就丢弃或标 `unverified`。
- **排名**：先按与需求的相关度，同相关度按人气与新鲜度。只留 **top 5**——宁可少而精，不拿低星凑数。

### 5. 呈现（每个候选一块）

```
**<技能名>** — <一句话描述>
  Source: GitHub | Awesome | 其他   ·   ★<stars> · 最近推送 <YYYY-MM>
  Link: <仓库 URL>
  人气: <高人气 / 正常 / ⚠ 早期项目慎用 / 维护停滞>
  Why: <一句话：如何匹配需求>
  安装: <复制粘贴即可执行的安装指令>
```

**安装指令（QunWork 两种方式，任选其一）**：
```
方式 A（推荐，目录复制）：
  git clone <仓库URL> 或用浏览器下载 zip 解压，把其中的 <技能目录>（含 SKILL.md 的文件夹）
  复制到  C:\Users\<用户>\AppData\Roaming\coworker\skills\<技能名>\   （本机所有会话可用）
  或复制到  <当前工作区>\.coworker\skills\<技能名>\                    （仅当前工作区）
方式 B（zip 导入技能市场）：
  把技能文件夹打成 <技能名>.zip（SKILL.md 在 zip 根或单层目录内），在 GUI 技能市场点"导入技能"。
```

**来源降级注明**（如 GitHub API 限流改用 web_search）：一行脚注。

## 边界情况

- **无匹配** → 直说没有，建议更宽的关键词。
- **全部低于人气门槛** → 不硬凑。说明该主题还没有成熟技能；确实很强的主题契合可提一个低星候选并显式标注 `早期项目 (N★)`，让它绝不像是经过验证的推荐。
- **全部命中已装** → 告诉用户已被覆盖，点名已有技能；不硬凑弱候选。
- **来源报错**（网络/限流）→ 注明哪个源失败，继续其余。绝不整体失败。

## 重要规则

- 只发现与推荐，**不自动安装**、不克隆、不改系统——安装动作留给用户执行。
- 避免重复装是第一要务：已装的绝不推荐为新。
- 人气门槛如实标注，1000+/100 两档硬性执行；无维护信号要降权。
- 候选必须是真的技能（有 SKILL.md），验证不了就丢弃或标 unverified。
- 本机网络注意：github.com 本体不通，一律走 web_fetch 代理通道或 raw.githubusercontent.com（可 curl）。
