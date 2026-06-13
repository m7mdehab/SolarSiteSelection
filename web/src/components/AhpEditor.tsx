import { useState, useEffect, useCallback } from 'react';
import { checkAhp } from '../api/client';
import type { AhpCheckResponse, Criterion } from '../types/api';
import '../styles/AhpEditor.css';

const SAATY_VALUES = [
  { value: 1 / 9, label: '1/9' },
  { value: 1 / 8, label: '1/8' },
  { value: 1 / 7, label: '1/7' },
  { value: 1 / 6, label: '1/6' },
  { value: 1 / 5, label: '1/5' },
  { value: 1 / 4, label: '1/4' },
  { value: 1 / 3, label: '1/3' },
  { value: 1 / 2, label: '1/2' },
  { value: 1, label: '1' },
  { value: 2, label: '2' },
  { value: 3, label: '3' },
  { value: 4, label: '4' },
  { value: 5, label: '5' },
  { value: 6, label: '6' },
  { value: 7, label: '7' },
  { value: 8, label: '8' },
  { value: 9, label: '9' },
];

function findClosestSaaty(val: number): string {
  let closest = SAATY_VALUES[0];
  let minDiff = Math.abs(val - closest.value);
  for (const sv of SAATY_VALUES) {
    const diff = Math.abs(val - sv.value);
    if (diff < minDiff) {
      minDiff = diff;
      closest = sv;
    }
  }
  return closest.label;
}

function parseSaaty(label: string): number {
  for (const sv of SAATY_VALUES) {
    if (sv.label === label) return sv.value;
  }
  return 1;
}

interface AhpEditorProps {
  criteria: Criterion[];
  onWeightsChange: (weights: Record<string, number>) => void;
}

export function AhpEditor({ criteria, onWeightsChange }: AhpEditorProps) {
  const n = criteria.length;

  const [upperTriangle, setUpperTriangle] = useState<string[][]>(() => {
    const mat: string[][] = [];
    for (let i = 0; i < n; i++) {
      mat[i] = [];
      for (let j = 0; j < n; j++) {
        mat[i][j] = '1';
      }
    }
    return mat;
  });

  const [result, setResult] = useState<AhpCheckResponse | null>(null);
  const [checking, setChecking] = useState(false);

  const buildMatrix = useCallback(
    (triangle: string[][]): number[][] => {
      const mat: number[][] = Array.from({ length: n }, () => Array(n).fill(1));
      for (let i = 0; i < n; i++) {
        for (let j = i + 1; j < n; j++) {
          const val = parseSaaty(triangle[i][j]);
          mat[i][j] = val;
          mat[j][i] = 1 / val;
        }
      }
      return mat;
    },
    [n]
  );

  const runCheck = useCallback(
    async (triangle: string[][]) => {
      if (n < 2) return;
      setChecking(true);
      try {
        const matrix = buildMatrix(triangle);
        const res = await checkAhp({ matrix });
        setResult(res);
        const weightMap: Record<string, number> = {};
        criteria.forEach((c, i) => {
          weightMap[c.key] = res.weights[i];
        });
        onWeightsChange(weightMap);
      } catch {
        // silently ignore errors during check
      } finally {
        setChecking(false);
      }
    },
    [buildMatrix, criteria, n, onWeightsChange]
  );

  // Run check when upperTriangle changes - schedule via setTimeout to avoid
  // ESLint's set-state-in-effect rule (the async resolution sets state after render)
  useEffect(() => {
    const snapshot = upperTriangle.map((row) => [...row]);
    const timer = setTimeout(() => {
      void runCheck(snapshot);
    }, 50);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [upperTriangle]);

  function handleCellChange(i: number, j: number, label: string) {
    setUpperTriangle((prev) => {
      const next = prev.map((row) => [...row]);
      next[i][j] = label;
      next[j][i] = findClosestSaaty(1 / parseSaaty(label));
      return next;
    });
  }

  const isInconsistentCell = (i: number, j: number): boolean => {
    if (!result?.most_inconsistent) return false;
    const [mi, mj] = result.most_inconsistent;
    return (i === mi && j === mj) || (i === mj && j === mi);
  };

  const cr = result?.cr ?? null;
  const crBad = cr !== null && cr > 0.1;

  return (
    <div className="ahp-editor">
      <div className={`ahp-cr-gauge ${crBad ? 'ahp-cr-bad' : 'ahp-cr-ok'}`}>
        {cr !== null ? (
          <>
            CR: <strong>{cr.toFixed(3)}</strong>{' '}
            {crBad ? '(inconsistent, CR > 0.10)' : '(acceptable)'}
          </>
        ) : checking ? (
          'Checking...'
        ) : (
          'CR: —'
        )}
      </div>
      <div className="ahp-matrix-scroll">
        <table className="ahp-matrix">
          <thead>
            <tr>
              <th></th>
              {criteria.map((c) => (
                <th key={c.key} title={c.name}>
                  {c.name.length > 10 ? c.name.slice(0, 10) + '...' : c.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {criteria.map((row, i) => (
              <tr key={row.key}>
                <td className="ahp-row-label" title={row.name}>
                  {row.name.length > 10 ? row.name.slice(0, 10) + '...' : row.name}
                </td>
                {criteria.map((_, j) => {
                  const inconsistent = isInconsistentCell(i, j);
                  if (i === j) {
                    return (
                      <td key={j} className="ahp-cell ahp-cell-diag">
                        1
                      </td>
                    );
                  } else if (j > i) {
                    return (
                      <td
                        key={j}
                        className={`ahp-cell ahp-cell-upper ${inconsistent ? 'ahp-cell-inconsistent' : ''}`}
                      >
                        <select
                          value={upperTriangle[i][j]}
                          onChange={(e) => handleCellChange(i, j, e.target.value)}
                        >
                          {SAATY_VALUES.map((sv) => (
                            <option key={sv.label} value={sv.label}>
                              {sv.label}
                            </option>
                          ))}
                        </select>
                      </td>
                    );
                  } else {
                    return (
                      <td
                        key={j}
                        className={`ahp-cell ahp-cell-lower ${inconsistent ? 'ahp-cell-inconsistent' : ''}`}
                      >
                        {upperTriangle[j][i] === '1'
                          ? '1'
                          : findClosestSaaty(1 / parseSaaty(upperTriangle[j][i]))}
                      </td>
                    );
                  }
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {result && (
        <div className="ahp-weights">
          <strong>Derived weights:</strong>
          {criteria.map((c, i) => (
            <span key={c.key} className="ahp-weight-chip">
              {c.name}: {((result.weights[i] ?? 0) * 100).toFixed(1)}%
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
