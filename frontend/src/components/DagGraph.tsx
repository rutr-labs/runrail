import { useMemo } from 'react';

/** Read-only dependency graph. Layout is layered left-to-right: a task's column
 *  is one past its deepest dependency, so parallel branches stack vertically.
 *  With `statuses` (task name -> run status) the graph becomes a live run view. */

export type DagTask = { name: string; task_type: string; depends_on: string[] };

const NODE_W = 168;
const NODE_H = 50;
const GAP_X = 64;
const GAP_Y = 18;
const PAD = 16;

const TYPE_LABELS: Record<string, string> = { shell: '›_', python: 'Py', notebook: 'Nb', sql: 'SQL' };
const TYPE_FILL: Record<string, string> = {
  shell: 'rgba(245,158,11,0.16)', python: 'rgba(59,130,246,0.16)',
  notebook: 'rgba(168,85,247,0.16)', sql: 'rgba(6,182,212,0.16)',
};
const TYPE_TEXT: Record<string, string> = {
  shell: '#f59e0b', python: '#3b82f6', notebook: '#a855f7', sql: '#06b6d4',
};

type Node = DagTask & { x: number; y: number; level: number };

function layout(tasks: DagTask[]): { nodes: Node[]; width: number; height: number } {
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
        x: PAD + level * (NODE_W + GAP_X),
        y: top + index * (NODE_H + GAP_Y),
      });
    });
  }
  const maxLevel = Math.max(...[...columns.keys()], 0);
  const width = PAD * 2 + (maxLevel + 1) * NODE_W + maxLevel * GAP_X;
  return { nodes, width, height };
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
  const { nodes, width, height } = useMemo(() => layout(tasks), [tasks]);
  if (!tasks.length) return null;
  const positions = new Map(nodes.map(n => [n.name, n]));

  return (
    <div className="dag-scroll">
      <svg className="dag" viewBox={`0 0 ${width} ${height}`} width={width} height={height}
           role="img" aria-label="Task dependency graph">
        {nodes.flatMap(node => (node.depends_on ?? []).map(dep => {
          const from = positions.get(dep);
          if (!from) return null;
          const x1 = from.x + NODE_W, y1 = from.y + NODE_H / 2;
          const x2 = node.x, y2 = node.y + NODE_H / 2;
          const mid = (x1 + x2) / 2;
          const active = statuses?.[node.name] === 'running';
          const done = statuses?.[dep] === 'success';
          return (
            <path key={`${dep}->${node.name}`}
              d={`M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`}
              className={`dag-edge${active ? ' dag-edge-active' : ''}${done ? ' dag-edge-done' : ''}`} />
          );
        }))}
        {nodes.map(node => {
          const status = statuses?.[node.name];
          const stroke = (status && STATUS_STROKE[status]) || 'var(--border-strong)';
          return (
            <g key={node.name} transform={`translate(${node.x}, ${node.y})`}
               className={`dag-node${status === 'running' ? ' dag-node-running' : ''}${onSelect ? ' dag-node-clickable' : ''}`}
               onClick={() => onSelect?.(node.name)}>
              <rect width={NODE_W} height={NODE_H} rx={10} className="dag-node-body" style={{ stroke }} />
              <rect x={10} y={13} width={26} height={24} rx={6}
                    fill={TYPE_FILL[node.task_type] ?? TYPE_FILL.shell} />
              <text x={23} y={29} textAnchor="middle" className="dag-node-type"
                    fill={TYPE_TEXT[node.task_type] ?? TYPE_TEXT.shell}>
                {TYPE_LABELS[node.task_type] ?? '?'}
              </text>
              <text x={44} y={24} className="dag-node-name">
                {node.name.length > 16 ? `${node.name.slice(0, 15)}…` : node.name}
              </text>
              <text x={44} y={38} className="dag-node-status">
                {status ?? node.task_type}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
