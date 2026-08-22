import {
  AbsoluteFill,
  Easing,
  interpolate,
  Sequence,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

/**
 * 一个可直接运行的示例 Composition（拷入新 Remotion 项目 src/ 即可用）。
 * 展示：入场淡入+缩放、标题、副标题、结尾淡出、多场景 Sequence 排布。
 *
 * 使用：把本文件拷到 src/HelloComposition.tsx，并在 Root.tsx 注册
 * （见同目录 Root.tsx）。
 */
export const HelloComposition: React.FC<{ title: string; subtitle: string }> = ({
  title = "Hello Remotion",
  subtitle = "React 驱动的视频",
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // 淡入（0-1s），保持到结尾
  const opacity = interpolate(frame, [0, 1 * fps], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  });

  // 标题缩放入场（0-1.5s）
  const scale = interpolate(frame, [0, 1.5 * fps], [0.85, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.spring({ damping: 200 }),
  });

  // 副标题延迟到 1s 出现
  const subtitleOpacity = interpolate(frame, [1 * fps, 2 * fps], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  });

  return (
    <AbsoluteFill
      style={{
        justifyContent: "center",
        alignItems: "center",
        backgroundColor: "#0b1020",
        fontFamily: "sans-serif",
        opacity,
      }}
    >
      <div
        style={{
          fontSize: 96,
          fontWeight: 700,
          color: "#ffffff",
          scale: String(scale),
          textAlign: "center",
        }}
      >
        {title}
      </div>
      <div
        style={{
          fontSize: 40,
          color: "#8ab4f8",
          marginTop: 24,
          opacity: subtitleOpacity,
        }}
      >
        {subtitle}
      </div>
    </AbsoluteFill>
  );
};

/**
 * 多场景示例：Sequence 依次排布两个场景。
 * 场景 1 占 0-150 帧，场景 2 从 150 帧开始。
 */
export const TwoScenes: React.FC = () => {
  const { fps } = useVideoConfig();
  return (
    <>
      <Sequence from={0} durationInFrames={150}>
        <HelloComposition title="Scene 1" subtitle="第一幕" />
      </Sequence>
      <Sequence from={150} durationInFrames={150}>
        <HelloComposition title="Scene 2" subtitle="第二幕" />
      </Sequence>
    </>
  );
};
