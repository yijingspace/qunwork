# QunWork 内置 skill：img2threejs（图片 → 程序化 Three.js 模型）

> 内置日期：2026-08-18 · 来源：github.com/img2threejs/img2threejs v1.4.4（Apache-2.0）
> 位置：`coworker/skills/img2threejs/`（内置只读层，随安装包分发）

## 能力

把参考图（物体/角色/CS2 皮肤）重建为 **code-only 程序化 Three.js 模型**，分阶段
雕刻流水线 + AI-vision 自纠错循环：

```
图片 → 质量契约 → spec → blockout → structure → form → material → lighting → interaction → optimization
```

- 非摄影测量、非网格提取、非下载素材——纯代码重建
- 质量门控：每 pass 截图对比参考图，身份特征不符即 fail
- 支持 generic / character / cs2 三种 profile
- 本地状态机（`.img2threejs/state.json`）跨会话可续跑
- 纯 Python 3.10+ 标准库，零三方依赖

## 使用方式

Agent 在会话中通过 `load_skill("img2threejs")` 加载（catalog 里可见），随后按 SKILL.md
流程执行：

```bash
# 初始化状态（从 skill 根目录运行）
python3 forge/state.py init --state .img2threejs/state.json --reference <图片> --profile generic
# 每一步前查询"下一步该做什么"
python3 forge/next.py --state .img2threejs/state.json
```

脚本全部相对 skill 根目录解析，Agent 拿到 `resources_path` 后直接执行。

## 打包

`packaging/qunwork-server.spec` 已把整个内置技能目录作为 data 打进 sidecar
（`coworker/skills/` → `_internal/coworker/skills/`）——此前 `collect_submodules`
只收 import 模块进 PYZ，SKILL.md/forge/grimoire 这些数据文件会丢失（vision skill
打包后缺 SKILL.md 的既有问题一并修复）。

## 验证

| 项目 | 结果 |
|---|---|
| SkillLoader 发现 | ✅ v1.4.4，48KB 指令 |
| forge 自带测试 | ✅ 673 passed（UTF-8 模式；Windows 默认 GBK 下 22 个 read_text 编码错，非逻辑缺陷） |
| next.py 状态机 | ✅ init → 分步清单 → 精确下一步命令正常 |
| 只读保护 | ✅ save_skill 不写入内置层（写入用户层） |
| 健康检查 | ✅ 无孤儿/碰撞（唯一 orphan 是 __pycache__ 噪音） |
| 安全评分 | 100 (critical)——代码生成器本质（62 处 subprocess/10 处 compile 是功能），仅标记不拦截加载 |

## 说明

- SKILL.md 提到的浏览器截图门（visual gate）依赖宿主环境能力：QunWork 侧由
  agent 的浏览器/截图工具承担，无浏览器时相应 pass 会停在证据不足并明确说明。
- integrations/vision 是可选适配器（SAM2/MediaPipe/Depth Anything），需要额外
  Python 环境，默认不启用；核心 forge 流程无需它们。
