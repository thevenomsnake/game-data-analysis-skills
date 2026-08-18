# Game Data Analysis Skills

**Una colección de Skills enchufables para analizar datos de juegos en Codex.**

[Sitio oficial](https://fairy.sumimi.jp/) · [English](README.md) · [简体中文](README.zh-CN.md)

Game Data Analysis Skills reúne Skills que pueden usarse por separado o combinarse según la tarea.
También puedes usar la colección sin Fairy. Fairy es una capa de producto independiente que organiza
las Skills elegidas en un flujo de trabajo para el equipo; no es una dependencia obligatoria.

## Qué incluye

| Módulo | Qué resuelve |
| --- | --- |
| **Setup** | Usa Git como base y permite configurar GitHub, GitLab, Git autoalojado, SSH, Git local y fuentes de planificación Git/SVN/local/none. |
| **Espacio de trabajo SQL** | Guarda cada consulta como una versión inmutable y buscable, con metadatos, hash y receipt exacto. |
| **Reglas y conocimiento** | Separa definiciones de eventos, entradas de planificación, referencias confirmadas y canonical rules. |
| **Ciclo de vida de consultas** | Lleva la solicitud hasta QUERY, validación, paquetes de activos formales y derivados de Dashboard con sus evidencias. |
| **Review y health** | Revisa el significado del producto y la estructura SQL, y detecta deriva antes de entregar. |
| **Resultados y lineage** | Vincula resultados, visualizaciones y libros de trabajo con la versión SQL exacta que los produjo. |
| **Superficies de ejecución** | Ejecuta SQL con DB-API/CLI, un adaptador web configurado o una entrega manual explícita. |
| **Visualizador de informes Excel** | Inspecciona un libro local y genera una presentación reutilizable sin conexión. No incluye libros reales. |

## Instalar y probar la primera consulta

Necesitas Python 3.11 o posterior y Git. La primera ejecución no requiere paquetes Python adicionales.

```powershell
git clone https://github.com/thevenomsnake/game-data-analysis-skills.git
Set-Location .\game-data-analysis-skills

python .\setup\scripts\bootstrap_repo.py configure `
  --root . `
  --remote https://github.com/thevenomsnake/game-data-analysis-skills.git `
  --planning-provider none
python .\setup\scripts\bootstrap_repo.py demo --root .
Copy-Item -Recurse .\setup "$HOME\.codex\skills\setup"
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

Actualiza Codex y usa `$sql-engineering`. Puedes probar el flujo basado en archivos sin una base de datos:

```powershell
python .\sql-engineering\scripts\sql_workspace.py save `
  --root .\sql-projects\example `
  --sql-file .\sql-engineering\assets\examples\daily-active-users.sql `
  --title "Daily active users" `
  --summary "Cuenta usuarios ficticios por fecha." `
  --kind temporary `
  --slug daily-active-users
```

El comando devuelve la ruta inmutable `v001.sql`. Ejecuta `receipt` sobre esa ruta antes de compartirla.
Sin una superficie automática, el resultado es `manual_required`; generar SQL no significa haberlo ejecutado.

## Inicializar la superficie de ejecución

Declara la intención al inicializar el proyecto formal:

```powershell
python .\sql-engineering\scripts\local_setup.py init `
  --repo-root . `
  --project example `
  --execution-surface web
```

- `direct`: perfil local de DB-API o CLI en modo de solo lectura.
- `web`: `web_query_adapter_v1` ignorado por Git y la sesión Chrome del usuario.
- `manual`: entrega la ruta exacta del SQL para que la persona lo ejecute y devuelva el resultado.

El repositorio incluye un ejemplo para Deltaverse. Para otro sitio, sigue la [guía de superficies y adaptadores web](sql-engineering/references/execution-surfaces.md): cambia en la configuración local las URL, los localizadores de la interfaz, las señales de finalización y la ruta de exportación. No automatiza el inicio de sesión ni guarda cookies.

## Dónde se encuentran SQL y activos

- `sql-projects/<project>/query_workspace/`: SQL temporal e histórico; se busca con `sql_query_workspace.py search` y no se versiona en Git.
- `sql-projects/<project>/formal_assets/`: paquetes compartidos de SQL formal, resultados, validaciones y Dashboard; usa `sql_repository.py build|serve` para consultarlos.
- Provider Snapshot, esquemas de Catalog y receipts: interfaces de identidad, rutas y hashes para consumidores externos de solo lectura.

## Elegir la fuente de planificación

El remote del repositorio y la fuente de planificación son decisiones independientes:

```powershell
python .\setup\scripts\bootstrap_repo.py configure --root . `
  --planning-provider git `
  --planning-url <git-planning-url> `
  --planning-branch main `
  --planning-id planning
python .\setup\scripts\bootstrap_repo.py planning-sync --root .

python .\setup\scripts\bootstrap_repo.py configure --root . `
  --planning-provider svn `
  --planning-url <svn-url> `
  --planning-revision <revision>

python .\setup\scripts\bootstrap_repo.py configure --root . `
  --planning-provider local `
  --planning-path <folder>
```

Usa `--planning-provider none` mientras no haya una fuente lista. Las credenciales quedan en el
mecanismo local de Git/SVN y no en la configuración pública.

## Seguridad y licencias

El árbol público solo contiene ejemplos ficticios: no incluye SQL de producción, resultados, esquemas
privados ni credenciales. El SQL externo se conserva como entrada inmutable. Las licencias de las
bibliotecas incluidas en el visualizador Excel están en [THIRD_PARTY_NOTICES.md](excel-report-visualizer/THIRD_PARTY_NOTICES.md).

## Para seguir leyendo

- [Setup onboarding](setup/references/onboarding.md)
- [SQL Engineering contract](sql-engineering/SKILL.md)
- [Manual de usuario](docs/USER_MANUAL.md)
- [Guía de consumo de activos de solo lectura](docs/READONLY_ASSET_CONSUMER_GUIDE.md)
- [Mantenimiento público](docs/PUBLIC_MAINTENANCE.md)
- [Visualizador de informes Excel](excel-report-visualizer/README.md)

## Próximos pasos

- Generar informes periódicos según una programación.
- Comparar resultados entre activos de datos.
- Seguir el origen de las anomalías e investigar sus causas.

Publicado bajo Apache License 2.0.
