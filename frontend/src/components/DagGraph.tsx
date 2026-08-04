import { useMemo } from 'react';

/** Read-only dependency graph. Layout is layered left-to-right: a task's column
 *  is one past its deepest dependency, so parallel branches stack vertically.
 *  With `statuses` (task name -> run status) the graph becomes a live run view. */

export type DagTask = { name: string; task_type: string; depends_on: string[] };

const NODE_H = 66;
const GAP_X = 76;
const GAP_Y = 22;
const PAD = 20;

const TYPE_LABELS: Record<string, string> = { shell: '›_', python: 'Py', notebook: 'Nb', sql: 'SQL' };
const TYPE_FILL: Record<string, string> = {
  shell: 'rgba(245,158,11,0.16)', python: 'rgba(59,130,246,0.16)',
  notebook: 'rgba(168,85,247,0.16)', sql: 'rgba(6,182,212,0.16)',
};
const TYPE_TEXT: Record<string, string> = {
  shell: '#f59e0b', python: '#3b82f6', notebook: '#a855f7', sql: '#06b6d4',
};

type Node = DagTask & { x: number; y: number; level: number };

// Glyph-aware width estimate for 14px/600 Inter: uppercase and digits run
// ~9.5px, the rest ~7.6px. A flat per-char budget under-measured all-caps
// names enough for them to paint past the node border into the edge corridor.
function estWidth(name: string): number {
  let w = 0;
  for (const ch of name) w += /[A-Z0-9]/.test(ch) ? 9.5 : 7.6;
  return w;
}

function layout(tasks: DagTask[]): { nodes: Node[]; width: number; height: number; nodeW: number } {
  // Width follows the longest task name (estWidth past the 66px chrome),
  // clamped to 210–320 so short DAGs keep their proportions and long names
  // widen the card instead of truncating at a fixed 18 chars.
  const longestName = Math.max(...tasks.map(t => estWidth(t.name)), 0);
  const nodeW = Math.max(210, Math.min(320, 66 + longestName));
  const byName = new Map(tasks.map(t => [t.name, t]));
  const levels = new Map<string, number>();
  const levelOf = (name: string, seen: Set<string>): number => {
    if (levels.has(name)) return levels.get(name)!;
    if (seen.has(name)) return 0; // cycle safety — the API rejects real cycles
    seen.add(name);
    const task = byName.get(name);
    const deps = (task?.depends_on ?? []).filter(d => byName.has(d));
    const level = deps.length ? 1 + Math.max(...deps.map(d => levelOf(d, seen))) : 0;
    levels.set(name, level);
    return level;
  };
  tasks.forEach(t => levelOf(t.name, new Set()));

  const columns = new Map<number, DagTask[]>();
  for (const task of tasks) {
    const level = levels.get(task.name) ?? 0;
    columns.set(level, [...(columns.get(level) ?? []), task]);
  }
  const maxRows = Math.max(...[...columns.values()].map(c => c.length), 1);
  const height = maxRows * NODE_H + (maxRows - 1) * GAP_Y + PAD * 2;
  const nodes: Node[] = [];
  for (const [level, column] of columns) {
    const columnHeight = column.length * NODE_H + (column.length - 1) * GAP_Y;
    const top = (height - columnHeight) / 2;
    column.forEach((task, index) => {
      nodes.push({
        ...task, level,
        x: PAD + level * (nodeW + GAP_X),
        y: top + index * (NODE_H + GAP_Y),
      });
    });
  }
  const maxLevel = Math.max(...[...columns.keys()], 0);
  const width = PAD * 2 + (maxLevel + 1) * nodeW + maxLevel * GAP_X;
  return { nodes, width, height, nodeW };
}

const STATUS_STROKE: Record<string, string> = {
  running: 'var(--running)', success: 'var(--success)', failed: 'var(--danger)',
  skipped: 'var(--queued)', cancelled: 'var(--queued)', queued: 'var(--warning)',
};

export function DagGraph({ tasks, statuses, onSelect }: {
  tasks: DagTask[];
  statuses?: Record<string, string>;
  onSelect?: (name: string) => void;
}) {
  const { nodes, width, height, nodeW } = useMemo(() => layout(tasks), [tasks]);
  if (!tasks.length) return null;
  const positions = new Map(nodes.map(n => [n.name, n]));
  // Truncation tracks the computed width with the same glyph-aware budget as
  // layout() (epsilon absorbs float drift so an exactly-fitting name is kept);
  // names only ellipsize once the 320px clamp bites.
  const budget = nodeW - 66 + 1e-6;
  const clip = (name: string): string => {
    if (estWidth(name) <= budget) return name;
    let w = 8; // reserve for the ellipsis glyph
    for (let i = 0; i < name.length; i++) {
      w += /[A-Z0-9]/.test(name[i]) ? 9.5 : 7.6;
      if (w > budget) return `${name.slice(0, i)}…`;
    }
    return name;
  };

  return (
    <div className="dag-scroll">
      <svg className="dag" viewBox={`0 0 ${width} ${height}`} width={width} height={height}
           role="img" aria-label="Task dependency graph">
        {nodes.flatMap(node => (node.depends_on ?? []).map(dep => {
          const from = positions.get(dep);
          if (!from) return null;
          const x1 = from.x + nodeW, y1 = from.y + NODE_H / 2;
          const x2 = node.x, y2 = node.y + NODE_H / 2;
          const mid = (x1 + x2) / 2;
          const active = statuses?.[node.name] === 'running';
          const done = statuses?.[dep] === 'success';
          return (
            <path key={`${dep}->${node.name}`}
              d={`M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`}
              className={`dag-edge dag-edge-enter${active ? ' dag-edge-active' : ''}${done ? ' dag-edge-done' : ''}`}
              style={{ animationDelay: `${Math.min(node.level, 5) * 80}ms` }} />
          );
        }))}
        {nodes.map(node => {
          const status = statuses?.[node.name];
          const stroke = (status && STATUS_STROKE[status]) || 'var(--border-strong)';
          return (
            <g key={node.name} transform={`translate(${node.x}, ${node.y})`}
               className={`dag-node dag-enter${status === 'running' ? ' dag-node-running' : ''}${onSelect ? ' dag-node-clickable' : ''}`}
               style={{ animationDelay: `${Math.min(node.level, 5) * 80}ms` }}
               onClick={() => onSelect?.(node.name)}>
              <title>{node.name}</title>
              <rect width={nodeW} height={NODE_H} rx={13} className="dag-node-body" style={{ stroke }} />
              <rect x={14} y={17} width={34} height={32} rx={8}
                    fill={TYPE_FILL[node.task_type] ?? TYPE_FILL.shell} />
              <text x={31} y={38} textAnchor="middle" className="dag-node-type"
                    fill={TYPE_TEXT[node.task_type] ?? TYPE_TEXT.shell}>
                {TYPE_LABELS[node.task_type] ?? '?'}
              </text>
              <text x={58} y={31} className="dag-node-name">
                {clip(node.name)}
              </text>
              <text x={58} y={48} className="dag-node-status">
                {status ?? node.task_type}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
