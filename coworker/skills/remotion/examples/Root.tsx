import { Composition } from "remotion";
import { HelloComposition, TwoScenes } from "./HelloComposition";

/**
 * Root.tsx 示例：注册上面两个 Composition 供渲染/预览。
 * 拷入项目 src/Root.tsx 覆盖（或并入现有 Root），并确保 src/index.ts
 * 以 <RemotionRoot/> 渲染本组件。
 */
export const RemotionRoot = () => {
  return (
    <>
      <Composition
        id="HelloComposition"
        component={HelloComposition}
        durationInFrames={150} // 5s @ 30fps
        fps={30}
        width={1920}
        height={1080}
        defaultProps={{ title: "Hello Remotion", subtitle: "React 驱动的视频" }}
      />
      <Composition
        id="TwoScenes"
        component={TwoScenes}
        durationInFrames={300} // 10s @ 30fps
        fps={30}
        width={1920}
        height={1080}
      />
    </>
  );
};
