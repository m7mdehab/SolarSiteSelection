import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
  /** Short label for which area failed, shown in the fallback. */
  label?: string;
  /** Optional custom fallback. */
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  message: string;
}

/**
 * Catches render-time errors in its subtree and shows a contained fallback
 * instead of unmounting the whole app to a blank white screen. Used to isolate
 * the results panel so one broken layer/field never takes down the entire UI.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, message: '' };

  static getDerivedStateFromError(err: unknown): State {
    return { hasError: true, message: err instanceof Error ? err.message : String(err) };
  }

  componentDidCatch(err: unknown, info: ErrorInfo): void {
    // Surface for diagnostics; do not rethrow.
    console.error('ErrorBoundary caught an error:', err, info.componentStack);
  }

  render(): ReactNode {
    if (this.state.hasError) {
      return (
        this.props.fallback ?? (
          <div className="error-boundary" data-testid="error-boundary" role="alert">
            <strong>This section couldn’t be displayed{this.props.label ? ` (${this.props.label})` : ''}.</strong>
            <p className="error-boundary-detail">{this.state.message}</p>
            <button onClick={() => this.setState({ hasError: false, message: '' })}>Dismiss</button>
          </div>
        )
      );
    }
    return this.props.children;
  }
}
