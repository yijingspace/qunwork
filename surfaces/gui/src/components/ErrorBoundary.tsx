import React from "react";

/**
 * ErrorBoundary — catches render-time exceptions in the subtree so a single
 * crashing panel (Markdown render, PDF parse, malformed tool args) doesn't
 * white-screen the whole app. Owner-audit 2026-08-07: the app had no boundary,
 * so any component throw killed the entire webview.
 *
 * Class component because React still requires componentDidCatch for boundaries
 * (no hook equivalent as of React 18).
 */
interface Props {
  children: React.ReactNode;
  fallback?: (error: Error, reset: () => void) => React.ReactNode;
}
interface State {
  error: Error | null;
}

export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo): void {
    // Best-effort console log; a real crash reporter can hook here later.
    // eslint-disable-next-line no-console
    console.error("[ErrorBoundary]", error, info?.componentStack);
  }

  reset = (): void => this.setState({ error: null });

  render(): React.ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (this.props.fallback) return this.props.fallback(error, this.reset);
    return (
      <div role="alert" aria-live="assertive" className="error-boundary">
        <div className="error-boundary-title">Something went wrong rendering this panel.</div>
        <div className="error-boundary-detail">{String(error?.message || error)}</div>
        <button className="error-boundary-retry" onClick={this.reset}>
          Retry
        </button>
      </div>
    );
  }
}

export default ErrorBoundary;