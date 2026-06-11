import { useMemo, useRef, useState } from "react";

import { ActivityFeed } from "./components/ActivityFeed";
import { CollectorUrlConfig } from "./components/CollectorUrlConfig";
import { HeroMetrics } from "./components/HeroMetrics";
import { MetricsPanel } from "./components/MetricsPanel";
import { TaskGrid } from "./components/TaskGrid";
import {
  outputTokensPerSec,
  recomputeCounters,
  useCollectorStream,
} from "./collector";
import { useCollectorUrl } from "./hooks/useCollectorUrl";
import { useConcurrency } from "./hooks/useConcurrency";

/** One dropdown entry: the folder is the event-grouping key; the label is
 * the project's display name (the source folder the harness runs from,
 * e.g. "yt-dlp"), falling back to the folder name. */
interface ProjectOption {
  folder: string;
  label: string;
}

function App() {
  const { url: collectorUrl, setUrl: setCollectorUrl } = useCollectorUrl();
  const { store, status } = useCollectorStream(collectorUrl);

  // The dashboard is project-agnostic: it offers whatever folders the
  // collector currently knows about (from manifests and/or events) and
  // scopes every panel to the picked one.
  const projects = useMemo<ProjectOption[]>(() => {
    const labels = new Map<string, string>();
    for (const [folder, clock] of Object.entries(store.folders)) {
      labels.set(folder, clock.project || folder);
    }
    for (const t of Object.values(store.tasks)) {
      if (!labels.has(t.folder)) labels.set(t.folder, t.folder);
    }
    const options = [...labels.entries()]
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
      .map(([folder, label]) => ({ folder, label }));
    // Two plan subfolders of the same project would collide on the display
    // name — disambiguate with the folder.
    const counts = new Map<string, number>();
    for (const o of options) counts.set(o.label, (counts.get(o.label) ?? 0) + 1);
    return options.map((o) =>
      (counts.get(o.label) ?? 0) > 1 && o.label !== o.folder
        ? { ...o, label: `${o.label} · ${o.folder}` }
        : o,
    );
  }, [store.folders, store.tasks]);

  const [picked, setPicked] = useState<string | null>(null);
  const project =
    picked !== null && projects.some((p) => p.folder === picked)
      ? picked
      : (projects[0]?.folder ?? null);

  const {
    target: agentsTarget,
    available: concurrencyAvailable,
    setTarget: setAgentsTarget,
  } = useConcurrency(collectorUrl, project);

  // Tasks keep store insertion order (manifest = files.txt order), so the
  // grid colors up in dispatch order without reshuffling.
  const tasks = useMemo(
    () => Object.values(store.tasks).filter((t) => t.folder === project),
    [store.tasks, project],
  );
  const counters = useMemo(
    () =>
      recomputeCounters(Object.fromEntries(tasks.map((t) => [t.task_id, t]))),
    [tasks],
  );
  // Live fleet output rate, held at the last reading when no active agent
  // is reporting (between waves / run drained) so the tile shows a
  // continuously updated number instead of flipping back to a placeholder.
  // Keyed by project so a switch starts fresh instead of showing the
  // previous project's frozen rate.
  const liveTokPerSec = useMemo(() => outputTokensPerSec(tasks), [tasks]);
  const lastTokPerSec = useRef<{ project: string | null; value: number } | null>(
    null,
  );
  if (liveTokPerSec !== null) {
    lastTokPerSec.current = { project, value: liveTokPerSec };
  }
  const outputTokPerSec =
    liveTokPerSec ??
    (lastTokPerSec.current?.project === project
      ? lastTokPerSec.current.value
      : null);
  const activity = useMemo(
    () => store.activity.filter((e) => e.folder === project),
    [store.activity, project],
  );
  const clock = project !== null ? store.folders[project] : undefined;

  return (
    <main className="flex h-screen flex-col gap-4 overflow-hidden p-4 md:p-6">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <ProjectTitle
          projects={projects}
          project={project}
          onPick={setPicked}
        />
        <div className="flex flex-wrap items-center gap-6">
          <CollectorUrlConfig url={collectorUrl} onChange={setCollectorUrl} />
          <ConnectionBadge status={status} />
        </div>
      </header>

      <HeroMetrics
        counters={counters}
        firstStartedTs={clock?.first_started_ts ?? null}
        lastTerminalTs={clock?.last_terminal_ts ?? null}
        agentsTarget={agentsTarget}
        onAgentsTargetChange={concurrencyAvailable ? setAgentsTarget : undefined}
        outputTokensPerSec={outputTokPerSec}
      />
      <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[1fr_340px]">
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
 * monitors one project, chosen from whatever the collector knows about.
 * Options are keyed by folder but display the project's name (the source
 * folder the harness runs from, e.g. "yt-dlp"). With nothing announced
 * yet there is no project to monitor.
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

function ConnectionBadge({ status }: { status: string }) {
  const dotColor =
    status === "open"
      ? "bg-status-complete"
      : status === "connecting"
        ? "bg-status-working"
        : status === "closed"
          ? "bg-status-failed"
          : "bg-status-idle";
  return (
    <div className="flex items-center gap-2 text-sm text-text-muted">
      <span className={`inline-block h-2 w-2 rounded-full ${dotColor}`} />
      {status}
    </div>
  );
}

export default App;
