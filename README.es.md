# Game Data Analysis Skills

Una edición pública del Skill SQL de Codex para uso local. Consulta [README.md](README.md) o
[README.zh-CN.md](README.zh-CN.md) y ejecuta:

```powershell
python .\setup\scripts\bootstrap_repo.py demo --root .
python .\setup\scripts\bootstrap_repo.py configure --root . --planning-provider none
Copy-Item -Recurse .\setup "$HOME\.codex\skills\setup"
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```


Solo contiene ejemplos ficticios y herramientas genéricas; no incluye `BetterXml`, resultados de
producción, esquemas privados ni credenciales.

## Próximos pasos

- Generar automáticamente informes periódicos según una programación.
- Comparar los resultados entre distintos activos de datos para comprobar si son razonables.
- Rastrear automáticamente el origen de las anomalías e investigar sus posibles causas.
