# README Maintenance Log

Append one entry for every public README release. Preserve earlier rows as historical evidence.

| Date | Version / commit | Scope | Verification |
|---|---|---|---|
| 2026-08-18 | `v1.4.4` / `4cbdcc0` | Split usage documentation into the Codex Skill interface and the external AI-agent/third-party command-and-file interface; added executable README maintenance validation. | README validator and tool tests passed; public boundary, capability registry, and six locale copy checks passed. |
| 2026-08-18 | `v1.4.3` / `52080d2` | Exposed stable `English`, `简体中文`, `繁體中文`, `日本語`, `한국어`, and `Español` navigation in every README. | Six locale `humanization copy`; `Public validation` passed on `main` and tag. |
| 2026-08-18 | `v1.4.1` / `379a16c` | Made the public README set product-neutral and described pluggable capability interfaces. | Six locale `humanization copy`; public boundary and source allowlist passed. |
| 2026-08-18 | `v1.4.0` / `1921fc1` | Replaced four locale stubs with complete README content; documented execution surfaces, asset interfaces, CI, source sync, security, and third-party notices. | Setup `7/7`; SQL `106/106`; six locale checks; public boundary and source allowlist passed. |

## Next Entry Template

```text
| YYYY-MM-DD | vX.Y.Z / <commit> | <facts, commands, interfaces, locales changed> | <checks and CI runs> |
```
