---
name: remotion
description: Make videos programmatically with Remotion (React -> MP4). Scaffold a project, animate compositions with useCurrentFrame/interpolate, and render videos or stills via the CLI. For building videos, motion graphics, animated charts, captioned clips, and video automation.
version: 1.0.0
category: media
author: qunwork
tags: video, react, animation, mp4, remotion, media, render
allowed-tools: read_file, write_file, apply_patch, replace_in_file, run_shell, web_fetch, web_search
---

# Remotion — 用 React 编程式做视频

Remotion（github.com/remotion-dev/remotion，remotion.dev）把视频当 React 组件来写：每个
Composition 是一个渲染树，按帧渲染，导出成真正的 MP4。适合自动生成视频：图表动画、
字幕片、标题卡、数据可视化视频、批量模板渲染。

关键事实：
- 语言/运行时：TypeScript + React，需要 Node 18+（本机已装 Node 24）。
- 核心包：`remotion`（框架 + 组件）、`@remotion/cli`（CLI）、`@remotion/media`（`<Video>/<Audio>` 等媒体组件，4.0.x 新 API）。
- 渲染原理：无头 Chrome（Headless Shell）逐帧截图 → FFmpeg 合成。首次渲染会自动下载 Chrome Headless Shell。
- 许可证：Remotion 有**特殊许可证**——个人/开源免费，**公司商业使用需购买公司许可证**（remotion.dev/license）。交付给公司项目前先确认授权。
- 官方给 AI agent 的技能仓库：github.com/remotion-dev/skills（`npx skills add remotion-dev/skills`），内容比本技能更细，遇到边界情况可去那里查对应 REFERENCE。

## 何时用这个技能

用户要「做个视频 / 动画 / 字幕片 / 数据动画 / 宣传片」且可以用程序化方式生成时。先判断：
- 纯视频剪辑（已有素材拼接、裁剪、转码）→ 优先 FFmpeg，不是 Remotion 的主场。
- 从数据/文案/设计稿生成动态视频、需要精确到帧的动画 → Remotion 合适。

## 0. 前置检查

```bash
node --version   # 需要 >= 18
```

## 1. 新建项目（无项目时）

```bash
npx create-video@latest --yes --blank --no-tailwind my-video
cd my-video
npm i
```

- `--blank` 给一个最小可运行的 Composition；`--no-tailwind` 跳过 Tailwind（需要再说）。
- 已有项目则跳过，直接在项目里加 Composition。
- 装额外包用 `npx remotion add <pkg>`（自动匹配版本），例如 `npx remotion add @remotion/media`、`npx remotion add @remotion/transitions`。

## 2. 项目结构

```
my-video/
  src/
    index.ts       # 注册根组件（<Root/>）
    Root.tsx       # <Composition/> 注册：id、尺寸、fps、时长、默认 props
    HelloWorld.tsx # 你的场景组件
  public/          # 静态资源（图片/字体/音频/视频），用 staticFile() 引用
  remotion.config.ts
```

`Root.tsx` 注册示例：

```tsx
import { Composition } from "remotion";
import { HelloWorld } from "./HelloWorld";

export const RemotionRoot = () => (
  <>
    <Composition
      id="HelloWorld"
      component={HelloWorld}
      durationInFrames={150}   // 150 帧 @30fps = 5 秒
      fps={30}
      width={1920}
      height={1080}
      defaultProps={{ title: "Hello" }}
    />
  </>
);
```

## 3. 动画三原则（最重要）

1. **用 `useCurrentFrame()` + `interpolate()` 驱动动画**。CSS `transition` / `animation`、Tailwind 动画类**不会在渲染时生效**，必须改成帧驱动。
2. **用 `Easing.bezier()` / `Easing.spring()` 调节奏**。
3. **style 里优先 `scale` / `translate` / `rotate` 属性，不要拼 `transform` 字符串**（Studio 里可编辑、渲染更稳）。

```tsx
import { AbsoluteFill, Easing, interpolate, useCurrentFrame } from "remotion";

export const FadeIn = () => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
      <div
        style={{
          opacity: interpolate(frame, [0, 60], [0, 1], {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
            easing: Easing.bezier(0.16, 1, 0.3, 1),
          }),
          scale: interpolate(frame, [0, 60], [0.8, 1], {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
          }),
          fontSize: 88,
        }}
      >
        Title
      </div>
    </AbsoluteFill>
  );
};
```

其他要点：
- 所有元素外层用 `AbsoluteFill`（绝对定位填满画布）做容器，里面用 flex 布局。
- 场景内给元素做交互标记用 `Interactive.Div`（可选，Studio 可编辑能力）。
- `useVideoConfig()` 取 `{ fps, width, height, durationInFrames }`——算时间用 `frame * fps` 表达秒。

## 4. 素材与媒体

- 图片/字体/音视频放 `public/`，代码里用 `staticFile("logo.png")` 引用；远程 URL 可直接传。
- 媒体组件（4.0.x 用 `@remotion/media`）：

```tsx
import { Audio, Video } from "@remotion/media";
import { staticFile, CanvasImage, AnimatedImage } from "remotion";

<Video src={staticFile("clip.mp4")} style={{ opacity: 0.5 }} />
<Audio src={staticFile("bgm.mp3")} />
<CanvasImage src={staticFile("logo.png")} style={{ width: 200, height: 200 }} />
<AnimatedImage src={staticFile("nyancat.gif")} />
```

- 时间轴控制：大多数组件支持 `from`（出现帧）、`durationInFrames`（持续帧）、`trimBefore`（素材内部裁剪开头）；不支持的组件包一层 `<Sequence from={} durationInFrames={}>`。
- 多场景视频：每个场景一个组件，用 `<Sequence from={累计帧}>` 依次排布，或用 `@remotion/transitions` 的 `<TransitionSeries>` 做转场。
- 中文字体：**必须显式加载字体**，否则中文可能渲染成方块。推荐 Google Fonts 方式（`@remotion/google-fonts` 或 `loadFont`），见官方 fonts 文档。

## 5. 渲染

只渲染当用户明确要求出片时。

```bash
# 渲染视频（不指定 composition 时用唯一/默认的）
npx remotion render HelloWorld out/hello.mp4

# 渲染单帧静态图（快速检查版式/颜色/某帧画面）
npx remotion still HelloWorld out/frame.png --frame=30 --scale=0.5

# 指定尺寸/码率/编码等
npx remotion render HelloWorld out/hello.mp4 --codec=h264 --crf=18
```

- 常用选项：`--codec=h264|h265|vp8|vp9|prores|gif`、`--crf`（质量，越低越好）、`--scale`（分辨率倍率，0.5 快速预览）、`--frames=0-149`（部分帧）。
- 透明背景视频：`npx remotion render HelloWorld out/hello.webm --codec=vp9 --pixel-format=yuva420p`（或看官方 transparent-videos 文档）。
- 渲染完成输出会打印文件路径，交付前确认文件存在且非 0 字节。

## 6. 预览

```bash
npx remotion studio --no-open
```

长驻进程，会打印 URL（默认 http://localhost:3000），访问 `/<composition-id>` 看指定场景。有浏览器环境就打开它给用户预览；没有就说明 URL 让用户自己开。

## 7. 常见坑

- **Chrome Headless Shell 下载失败/超时**：设置环境变量 `REMOTION_BROWSER_EXECUTABLE` 指向本机 Chrome 可执行文件，或 `npx remotion browser ensure` 手动装。
- **包版本不一致**：`remotion`、`@remotion/cli`、`@remotion/*` 必须同一版本。用 `npx remotion add` 装包可避免错版。
- **首帧/布局不对**：先 `npx remotion still <id> --frame=30 --scale=0.5` 出一帧图确认，再整片渲染，省时间。
- **动画闪烁/不流畅**：动画必须帧驱动（见第 3 节），`useCurrentFrame()` 之外不要依赖真实时间。
- **内存不足（大视频）**：`--concurrency=2` 降低并行；或先渲染小尺寸版本验证。
- **图片跨域**：远程图片若 CORS 受限，下载到 `public/` 再用 `staticFile()`。

## 8. 参考链接

- 文档：https://www.remotion.dev/docs
- CLI 选项：https://www.remotion.dev/docs/cli/render
- 官方 agent 技能：https://github.com/remotion-dev/skills
- 许可证：https://remotion.dev/license
- 本技能示例文件：见 `examples/` 目录（HelloComposition.tsx + Root.tsx，可拷入新项目直接改）。
