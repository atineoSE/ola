# ola-dashboard

The browser-based monitor for an ola run — a visually rich, pure-read view over
the agent-folder `.ola/*` files. See the `ola-dashboard` skill for the design
philosophy.

## The optional progress probe

Beyond the built-in counters and token throughput, the dashboard can surface a
**project-defined progress metric** (tests passing, files migrated, percent
coverage — whatever a run wants to watch climb). It renders as a hero tile with
a sparkline.

The metric comes from a **probe**: a command the *harness* runs **in the project
base branch, on merge-back progress**. The base branch only changes when a task
merges its worktree back, so the harness probes when that happens — once as a
baseline before the run, then after each merge-back — rather than on a fixed
timer that would just re-read an unchanged tree. The dashboard never runs the
probe; it only reads the samples the harness appends to
`<folder>/.ola/metrics.jsonl`.

### Probe contract

A probe is **any executable** that, when run, prints a JSON object to stdout:

```json
{"name": "tests passing", "value": 142}
```

- `name` is the metric label shown on the tile; `value` must be a **number**.
- To report **multiple metrics**, print a JSON **array** of such objects; the
  dashboard renders the first as the primary tile.
- The probe runs in the **project base branch** (its working directory is the
  project repo), so it measures the merged result of completed tasks — not any
  in-flight worktree.
- The probe must be **idempotent**: it is re-run after every merge-back (and
  once as a baseline), so running it back-to-back on an unchanged tree must be
  harmless and yield the same reading.
- The probe is expected to **run fast and do no network I/O** — it should be a
  cheap local read (count a file, grep a log, query a local socket), not a slow
  or remote call.

### Configuration

Configure the probe on the harness, via flag:

| Flag | Default | Meaning |
|------|---------|---------|
| `--metric-cmd <cmd>` | _unset_ | The probe command to run. |

There is no interval knob — the probe fires on merge-back progress, not a timer.

### Fallback when no probe is configured

If `--metric-cmd` is not set, the harness writes no `metrics.jsonl`, the
snapshot's `progress` field stays empty, and the dashboard renders **nothing
extra** — the standard layout (counters, clock, tokens/sec, heatmap, feed) is
unchanged. The progress tile is strictly additive.

---

# React + TypeScript + Vite

This template provides a minimal setup to get React working in Vite with HMR and some ESLint rules.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Oxc](https://oxc.rs)
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/)

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the ESLint configuration

If you are developing a production application, we recommend updating the configuration to enable type-aware lint rules:

```js
export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...

      // Remove tseslint.configs.recommended and replace with this
      tseslint.configs.recommendedTypeChecked,
      // Alternatively, use this for stricter rules
      tseslint.configs.strictTypeChecked,
      // Optionally, add this for stylistic rules
      tseslint.configs.stylisticTypeChecked,

      // Other configs...
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])
```

You can also install [eslint-plugin-react-x](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-x) and [eslint-plugin-react-dom](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-dom) for React-specific lint rules:

```js
// eslint.config.js
import reactX from 'eslint-plugin-react-x'
import reactDom from 'eslint-plugin-react-dom'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...
      // Enable lint rules for React
      reactX.configs['recommended-typescript'],
      // Enable lint rules for React DOM
      reactDom.configs.recommended,
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])
```
