import { useMemo, useState } from "react";

import { ActivityFeed } from "./components/ActivityFeed";
import { HeroMetrics } from "./components/HeroMetrics";
import { MetricsPanel } from "./components/MetricsPanel";
import { TaskGrid } from "./components/TaskGrid";
import { recomputeCounters, sumOutputTokens, useSnapshot } from "./snapshot";
import { agentColor, agentName, inkOn } from "./agent";
import { useConcurrency } from "./hooks/useConcurrency";
import { useOutputTokensPerSec } from "./hooks/useOutputTokensPerSec";

/** One dropdown entry: the folder is the event-grouping key; the label is
 * the folder's display name (its plan subfolder name). */
interface ProjectOption {
  folder: string;
  label: string;
}

function App() {
  const { snapshot, status } = useSnapshot();

  // The dashboard is project-agnostic: it offers whatever folders the
  // snapshot currently contains and scopes every panel to the picked one.
  const projects = useMemo<ProjectOption[]>(() => {
    const labels = new Map<string, string>();
    for (const [folder, clock] of Object.entries(snapshot.folders)) {
      labels.set(folder, clock.project || folder);
    }
    for (const t of Object.values(snapshot.tasks)) {
      if (!labels.has(t.folder)) labels.set(t.folder, t.folder);
    }
    const options = [...labels.entries()]
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
      .map(([folder, label]) => ({ folder, label }));
    // Two folders sharing a display name — disambiguate with the folder key.
    const counts = new Map<string, number>();
    for (const o of options) counts.set(o.label, (counts.get(o.label) ?? 0) + 1);
    return options.map((o) =>
      (counts.get(o.label) ?? 0) > 1 && o.label !== o.folder
        ? { ...o, label: `${o.label} · ${o.folder}` }
        : o,
    );
  }, [snapshot.folders, snapshot.tasks]);

  const [picked, setPicked] = useState<string | null>(null);
  const project =
    picked !== null && projects.some((p) => p.folder === picked)
      ? picked
      : (projects[0]?.folder ?? null);

  const {
    target: agentsTarget,
    available: concurrencyAvailable,
    setTarget: setAgentsTarget,
  } = useConcurrency(project);

  // Tasks keep snapshot insertion order (PLAN.md order), so the grid colors
  // up in dispatch order without reshuffling.
  const tasks = useMemo(
    () => Object.values(snapshot.tasks).filter((t) => t.folder === project),
    [snapshot.tasks, project],
  );
  const counters = useMemo(
    () =>
      recomputeCounters(Object.fromEntries(tasks.map((t) => [t.task_id, t]))),
    [tasks],
  );
  // Live fleet output rate: a windowed Δtokens/Δdecode across active agents
  // (see useOutputTokensPerSec). Held at the last reading across reporting
  // gaps and reset on a project switch, so the tile shows a continuously
  // updated number instead of the slowly-moving lifetime average.
  const outputTokPerSec = useOutputTokensPerSec(tasks, project);
  const totalOutputTokens = useMemo(() => sumOutputTokens(tasks), [tasks]);
  const activity = useMemo(
    () => snapshot.activity.filter((e) => e.folder === project),
    [snapshot.activity, project],
  );
  const clock = project !== null ? snapshot.folders[project] : undefined;

  // The run is themed by its agent backend: the agent's signature color fills
  // the page background (and stays the in-panel accent), the header switches to
  // a contrasting ink, and it names the agent and its model(s). Until a backend
  // is known the dashboard keeps its default dark theme.
  const backend = clock?.agent_backend ?? "";
  const accent = agentColor(backend);
  const themed = backend !== "";
  const ink = inkOn(accent);

  return (
    <main
      className="flex h-screen flex-col gap-4 overflow-hidden p-4 md:p-6"
      style={
        {
          "--color-accent": accent,
          ...(themed ? { background: accent } : {}),
        } as React.CSSProperties
      }
    >
      <header
        className="flex flex-wrap items-center justify-between gap-4"
        style={themed ? { color: ink } : undefined}
      >
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <ProjectTitle
            projects={projects}
            project={project}
            onPick={setPicked}
          />
          <AgentIdentity backend={backend} models={clock?.models ?? []} />
        </div>
        <ConnectionBadge status={status} />
      </header>

      <HeroMetrics
        counters={counters}
        activeElapsedSeconds={clock?.active_elapsed_s ?? null}
        activeAnchorTs={clock?.active_anchor_ts ?? null}
        agentsTarget={agentsTarget}
        onAgentsTargetChange={concurrencyAvailable ? setAgentsTarget : undefined}
        outputTokensPerSec={outputTokPerSec}
        totalOutputTokens={totalOutputTokens}
      />
      {/* Below lg the two columns collapse into a single column and stack
          vertically. Pin definite row tracks (not content-driven `auto` rows)
          so the h-full / flex-1 height chain resolves deterministically;
          otherwise the TaskGrid ResizeObserver and the stacked feed/metrics
          scrollbars feed back on each other and the lower panel flickers. */}
      <div className="grid min-h-0 flex-1 grid-rows-[minmax(0,1fr)_minmax(0,1fr)] gap-4 lg:grid-cols-[1fr_340px] lg:grid-rows-[minmax(0,1fr)]">
        <TaskGrid tasks={tasks} />
        <div className="flex min-h-0 flex-col gap-4">
          <ActivityFeed entries={activity} />
          <MetricsPanel tasks={tasks} />
        </div>
      </div>
    </main>
  );
}

interface ProjectTitleProps {
  projects: ProjectOption[];
  project: string | null;
  onPick: (folder: string) => void;
}

/**
 * The page title doubles as the project picker: each dashboard instance
 * monitors one folder, chosen from whatever the snapshot contains. With
 * nothing on disk yet there is no folder to monitor.
 */
function ProjectTitle({ projects, project, onPick }: ProjectTitleProps) {
  if (project === null) {
    return (
      <h1
        data-testid="project-title-empty"
        className="text-2xl font-semibold tracking-tight text-text-muted"
      >
        no project available
      </h1>
    );
  }
  return (
    <h1 className="text-2xl font-semibold tracking-tight">
      <select
        aria-label="project"
        data-testid="project-select"
        value={project}
        onChange={(ev) => onPick(ev.target.value)}
        className="cursor-pointer rounded border border-transparent bg-transparent font-semibold tracking-tight hover:border-border focus:border-accent focus:outline-none"
      >
        {projects.map((p) => (
          <option key={p.folder} value={p.folder} className="bg-bg-panel">
            {p.label}
          </option>
        ))}
      </select>
    </h1>
  );
}

interface AgentIdentityProps {
  backend: string;
  models: string[];
}

/**
 * The agent driving the picked run and the model(s) it is using. The whole
 * page is already tinted the agent's color, so the name needs no swatch — it
 * just rides the header's contrasting ink (bold for the name, dimmed mono for
 * the models). Both come from the folder's snapshot clock — `agent_backend`
 * from the event stream, `models` from STATS.jsonl. Renders nothing until a
 * backend is known, so an empty/just-started run shows only the title.
 */
function AgentIdentity({ backend, models }: AgentIdentityProps) {
  if (!backend) return null;
  return (
    <div
      data-testid="agent-identity"
      className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm"
    >
      <span data-testid="agent-name" className="font-semibold">
        {agentName(backend)}
      </span>
      {models.length > 0 && (
        <span
          data-testid="agent-model"
          className="font-mono opacity-80"
          title={models.join(", ")}
        >
          {models.join(", ")}
        </span>
      )}
    </div>
  );
}

function ConnectionBadge({ status }: { status: string }) {
  const dotColor =
    status === "open"
      ? "bg-status-complete"
      : status === "connecting"
        ? "bg-status-working"
        : "bg-status-failed";
  // Inherits the header's color (themed ink, or the default muted look off a
  // dark page) with a slight dim, so it reads on any agent background.
  return (
    <div className="flex items-center gap-2 text-sm opacity-80">
      <span className={`inline-block h-2 w-2 rounded-full ${dotColor}`} />
      {status}
    </div>
  );
}

export default App;
