"""ola-dashboard: a browser view over the same agent-folder files ola-top reads.

A thin, stateless HTTP server (:mod:`ola.dashboard.server`) serves the built
single-page app and a small JSON API that re-parses the agent folder per
request via :func:`ola.monitor.data.build_snapshot`. There is no collector and
no in-memory run state — the ``.ola/`` files are the source of truth, so the
server can be killed and restarted at any time without losing anything.
"""
