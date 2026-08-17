# Decisions

Public architecture decisions are kept short and generic. Project-specific rules belong in the
project Rule Store, not in this directory.

## Current decisions

- The public repository is a clean, independent release surface; internal project data is never
  copied into it.
- SQL, results, visualizations, and formal packages use exact versioned lineage.
- Local execution is optional and read-only; missing configuration falls back to manual handoff.
