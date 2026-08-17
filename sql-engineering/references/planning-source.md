# Planning Source Space

Use `PLANNING_SOURCE` for product/stage planning and config sources. Keep source control, source
release, Knowledge version, SQL project, and business rule as separate identities.

## Management And Providers

Configuration starts with one ownership choice and persists it locally:

| Management mode | Source input | Revision selected when called | Source mutation |
|---|---|---|---|
| `user_managed` | Local SVN working copy or non-SVN folder | User-selected local state; exact revision is required only for explicit sync | Never update, compare with remote HEAD, switch, clean, or overwrite the source |
| `tool_managed` | Canonical SVN URL plus a secure credential reference | Remote revision current at explicit `check`/`sync` | Never create or modify a user working copy |

This contract does not define who invokes the commands or when. Scheduling, deployment, and
consumer integration remain outside SQL Engineering.

Prefer `svn` when setup receives a valid SVN working copy or canonical SVN URL. Use `folder` only
for sources that are not version-controlled in SVN.

| Provider | PSR contents | Source bytes used by Knowledge |
|---|---|---|
| `svn` | Exact repository UUID, URL, revision, complete hash manifest, and diff; no duplicate source tree | Export the declared file at the PSR revision into `.local/`, verify its manifest hash, then preserve it in the Knowledge source snapshot |
| `folder` | Complete embedded folder snapshot plus manifest and diff | Read the sealed embedded file, then preserve it in the Knowledge source snapshot |

For `user_managed`, the local source is authoritative even when the user intentionally keeps an
older revision. Status and normal SQL work do not compare it with remote HEAD and do not require an
update. When the user explicitly requests sync, require a clean, complete, single-revision working
copy and export that exact committed revision. Local modifications, unversioned files, switched
paths, mixed revisions, and partial state never enter a PSR. For `tool_managed`, resolve the
configured URL when an explicit command runs. Both modes write staging only under the repository's
ignored `.local/` tree and block SVN externals.

## Storage

```text
planning-sources/<PRODUCT>/stages/<STAGE>/releases/<PSR-ID>/
  release.json
  files.json
  diff.json
  files/<complete source tree>  # folder provider and legacy v1 only

sql-projects/<PROJECT>/planning/source_binding.json
.local/planning-sources/<PROJECT>.json
.local/planning-source-export/
.local/planning-source-materialized/
```

Tracked SVN PSRs use `planning_source_release_v2` and `planning_source_binding_v2`. Existing v1
folder releases remain readable history. Do not migrate a v1 project binding implicitly during a
QUERY or any other consumer route; require one explicit `PLANNING_SOURCE sync`.

Tracked source-control receipts may contain the non-secret SVN repository root, canonical URL,
repository UUID, exact revision, and `working_copy_pinned|remote_latest` selection policy. Machine
paths, usernames, credential references, secrets, exported trees, scan caches, and materialized
source files remain local and never enter Git.

## Configure

Run `status` first. Present missing or modifiable configuration items, then configure only the
section selected by the user. The same command changes an existing configuration; QUERY and other
routes cannot do so.

User-managed local source:

```powershell
python .\sql-engineering\scripts\planning_source.py configure `
  --repo-root . --project DEMO_ANALYTICS --product EXAMPLE --stage BASE `
  --source-path <svn-working-copy> --provider auto `
  --management-mode user_managed `
  --function-selection PLANNING_SOURCE --user-request "<verbatim request>"
```

Tool-managed remote SVN source:

```powershell
python .\sql-engineering\scripts\planning_source.py configure `
  --repo-root . --project DEMO_ANALYTICS --product EXAMPLE --stage BASE `
  --svn-url <canonical-url> --provider svn --management-mode tool_managed `
  --svn-username <username> --credential-env <local-secret-variable> `
  --function-selection PLANNING_SOURCE --user-request "<verbatim request>"
```

Use `local_setup.py configure` for the normal member flow so the password is collected in the
private masked prompt and persisted locally. The low-level command accepts only a non-secret
environment-variable reference. SVN receives the password through stdin with auth caching disabled;
never use a URL credential, command argument, chat value, JSON value, or tracked file.

## Check And Sync

`check` is revision-first. If the active PSR already pins the current SVN content revision, return
unchanged without exporting or hashing the tree. Otherwise show repository/working-copy status and
a bounded changed-path preview when an earlier SVN revision exists.

```powershell
python .\sql-engineering\scripts\planning_source.py check `
  --repo-root . --project DEMO_ANALYTICS `
  --function-selection PLANNING_SOURCE --user-request "<verbatim request>"

python .\sql-engineering\scripts\planning_source.py sync `
  --repo-root . --project DEMO_ANALYTICS --reason "<reason>" `
  --function-selection PLANNING_SOURCE --user-request "<verbatim request>"
```

For user-managed SVN, explicit `check` and `sync` use the working-copy revision and never contact
remote HEAD to choose another revision. For tool-managed SVN they resolve remote latest only when
the command is called. `sync` exports the selected exact revision, hashes the complete tree, writes
the PSR manifests, verifies the binding transaction, and removes the export. It never runs
`svn update`. Identical provider identity and content produce no new release. A provider migration
creates a new PSR even when file content is unchanged so provenance is explicit; a zero-diff
migration does not force unrelated KDV refreshes. An older configured revision never rolls the
active release back implicitly.

A user-managed source may remain intentionally older than the repository. This is not a warning,
missing configuration, or QUERY blocker. Dirty, mixed, or partial diagnostics only describe
whether a new exact PSR can be created when sync is explicitly requested; the current binding
remains usable.

For folders, retain the existing complete-copy behavior while excluding source-control metadata,
editor/system files, and temporary Office files.

## Knowledge Boundary

QUERY, Review, Formalize, and Dashboard consume only the active KDV. They never invoke source
configuration or synchronization, and they never read the local source path. Run source
`check`/`sync` as a separate PLANNING_SOURCE action. After a new PSR, only projection specs whose
exact `relative_file` changed become pending. Keep the blocker until an explicit `KNOWLEDGE`
refresh has generated, reviewed, and bound the corresponding KDV. Source synchronization never
authorizes a Knowledge or canonical-rule change.

```text
SVN revision or sealed folder
  -> PSR manifest
  -> reviewed planning_projection_spec_v1
  -> immutable Knowledge source snapshot + KDV
  -> project Knowledge binding
  -> SQL knowledge_reference_v1
```

Daily SQL resolves only the active KDV. It never reads SVN, an Excel workbook, a PSR export cache,
or a machine source path directly.

## Validation

Validate folder PSRs by rehashing embedded files. Validate SVN PSRs offline by checking the sealed
file manifest, tree hash, source-control contract, binding, registry, and KDV alignment. During a
Knowledge refresh, materialize the exact declared SVN file and require its byte hash and size to
match `files.json` before extraction.

Local config v1/v2 does not encode management ownership. Report `management_mode_required` and
leave the active PSR/KDV untouched until the user explicitly runs configure. Never infer ownership
from `source_path`, provider, or an old sync policy.

## SourceAdapters Boundary

SourceAdapters is another Game Data Analysis Skills tool. It owns XML-description cleanup, not planning
sources. SQL Engineering never imports, calls, or deploys SourceAdapters.
