---
name: remotion-frame-qa
description: 无浏览器环境下校验 Remotion 动画视频画面：渲染关键静帧 + 用 pngjs 像素颜色探针脚本自动验证角色/文字/背景是否出现在预期位置。适合做动画视频时无法打开 Studio/预览页的 Windows 环境。
version: 0.1.0
category: general
---

# Remotion 静帧像素校验（无浏览器 QA）

无法打开 Remotion Studio 或 Playwright 不可用时，用「关键帧渲染 + 像素颜色探针」验证画面是否正确。

## 流程

1. **渲染关键静帧**（小尺寸加速）：
```bash
npx remotion still <CompId> out/f250.png --frame=250 --scale=0.5
```
挑每个场景的 1-2 个关键帧（开场、动作高潮、字幕出现后）。

2. **像素探针校验**：对每帧定义若干探针 `(x, y, w, h, [r,g,b], tol, min)`，统计区域内匹配颜色的像素数是否达标。脚本模板见下。位置用比例换算：渲染 scale=0.5 时，设计坐标 ×0.5。

3. **诊断技巧**：
- 探针 FAIL 先怀疑探针位置不对（角色在移动/尺寸不同），用横向扫描（统计区域明暗/颜色分布）确认元素实际位置，再决定改探针还是改布局。
- 真实布局 bug 特征：探针计数为 0（元素完全不在区域）或远低于预期。z 顺序问题（被遮挡）表现为计数接近 0。
- 网格采样 step=2 采样每第 2 个像素，校验够快（540x960 一帧 <1s）。

## 模板（node scripts/check_frames.mjs，需 npm i pngjs）

```js
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { PNG } from "pngjs";
const __dirname = path.dirname(fileURLToPath(import.meta.url));

function countInRegion(img, x, y, w, h, [r, g, b], tol) {
  let count = 0, step = 2;
  for (let py = y; py < Math.min(y + h, img.height); py += step) {
    const row = py * img.width;
    for (let px = x; px < Math.min(x + w, img.width); px += step) {
      const idx = (row + px) * 4;
      const dr = Math.abs(img.data[idx] - r), dg = Math.abs(img.data[idx + 1] - g), db = Math.abs(img.data[idx + 2] - b);
      if (dr <= tol && dg <= tol && db <= tol) count++;
    }
  }
  return count;
}
// 探针定义：[名称, x, y, w, h, [r,g,b], 容差, 最少像素数]
const probes = [["cat-orange", 330, 580, 190, 170, [245, 158, 11], 70, 800]];
for (const [n, x, y, w, h, col, tol, min] of probes) {
  const img = PNG.sync.read(fs.readFileSync(path.join(__dirname, "out/f250.png")));
  const cnt = countInRegion(img, x, y, w, h, col, tol);
  console.log(`${cnt >= min ? "OK  " : "FAIL"} ${n} (count=${cnt} min=${min})`);
}
```

## 备注（Windows 环境）
- `.mjs` 里用 `import` + `fileURLToPath`，不要用 `require`/`__dirname`。
- 不要用 PowerShell System.Drawing 逐像素循环（极慢，会超时）；用 Node + pngjs。
- 校验脚本和探针文件都是临时的，跑完可删。

