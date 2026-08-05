# Game Data Analysis Skills

**Un ciclo de vida SQL para Codex respaldado por archivos y ejecución de base de datos configurable de solo lectura.**

Game Data Analysis Skills convierte el trabajo SQL de una conversación en archivos de proyecto
duraderos. Cada consulta generada o modificada se guarda, versiona, indexa, se puede buscar y se
entrega mediante una ruta exacta, para que el trabajo siga siendo comprensible cuando termina el chat.

[English](README.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

> Un bloque SQL en el chat es una explicación. El archivo `vNNN.sql` verificado es el entregable.

## Qué problema resuelve

El SQL creado en un chat se pierde con facilidad. Una consulta útil suele copiarse a otro archivo,
editarse sin historial o separarse de la explicación de su propósito. Más adelante, nadie sabe qué
versión produjo un resultado o si se sobrescribió un archivo externo.

Este Skill proporciona a Codex un contrato de espacio de trabajo pequeño y verificable:

| Capacidad | Qué ocurre |
|---|---|
| Inicialización del repositorio | Crea una estructura estable `sql-projects/` y el primer proyecto |
| Gobierno del contexto | Versiona por separado telemetría original, entradas de diseño, material confirmado y reglas canónicas |
| Entrega de SQL | Guarda cada consulta generada o modificada como una versión inmutable `vNNN.sql` |
| Ejecución por entorno | Ejecuta SQL guardado mediante un controlador DB-API de solo lectura o una CLI de base de datos |
| Entrada de SQL externo | Trata el archivo recibido como entrada y trabaja con una copia dentro del proyecto |
| Historial consultable | Registra título, propósito, etiquetas, dialecto, ruta y hash de contenido |
| Control de revisiones | Mantiene correcciones y ampliaciones en una familia de consultas sin sobrescribir el historial |
| Recibo exacto | Verifica archivo, metadatos, índice y hash actual antes de entregar |
| Etiquetas de ciclo de vida | Distingue SQL temporal, reutilizable y orientado a dashboards |

La edición pública de la especificación no contiene esquemas de empresa, tablas de producción,
credenciales, reglas de negocio privadas, resultados de consultas ni integraciones internas de ejecución.

## Qué debes proporcionar para un proyecto

| Contexto necesario | Cómo lo gestiona el Skill |
|---|---|
| Definición original de telemetría | Copia XML, JSON, YAML, Excel, CSV, texto u otro formato sin modificarlo en `sources/raw/` y registra hash y versión |
| Base de datos y dialecto SQL | Declara por entorno el dialecto de generación; los datos locales DB-API o CLI quedan fuera de Git |
| Tablas de diseño y configuración | Conserva los originales en `knowledge/planning/`; son evidencia, no reglas automáticas |
| Material confirmado por una persona | Guarda en `knowledge/confirmed/` la versión revisada, quién la confirmó, el motivo y su procedencia |
| Reglas de negocio canónicas | Guarda Base, grano, cálculo, filtros y referencias confirmadas como versiones inmutables en `rules/definitions/` |

El Skill no inventa estos hechos del proyecto. Proporciona una estructura que hace visibles su propiedad y
sus cambios. Consulta la [guía de incorporación](sql-engineering/references/project-onboarding.md).

## Crea e incorpora un proyecto

### 1. Instala el Skill

Clona este repositorio y copia o enlaza `sql-engineering/` en el directorio de Skills de Codex:

```powershell
git clone https://github.com/thevenomsnake/game-data-analysis-skills.git
Set-Location .\game-data-analysis-skills
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

Reinicia o actualiza Codex. El Skill estará disponible como `$sql-engineering`.

### 2. Inicializa un espacio de trabajo

```powershell
python .\sql-engineering\scripts\sql_workspace.py bootstrap `
  --root . `
  --project-id example `
  --dialect starrocks
```

`bootstrap` inicializa `sql-projects/example` y crea catálogos vacíos de telemetría, conocimiento, reglas y
SQL. Al repetirlo, repara la estructura vacía ausente sin borrar contenido registrado.

### 3. Registra el contexto del proyecto

Entrega primero la telemetría original, las tablas de diseño/configuración y el material confirmado por
separado. Después declara el entorno y dialecto SQL y fija únicamente reglas confirmadas explícitamente.

```powershell
python .\sql-engineering\scripts\sql_workspace.py status `
  --root .\sql-projects\example
```

`query_context_ready=false` indica que falta una definición original de telemetría. No disponer de conexión
automática es válido; el proyecto usará la entrega manual del archivo SQL.

### 4. Pide el trabajo a Codex con lenguaje natural

```text
$sql-engineering Crea una consulta StarRocks que cuente usuarios de inicio de sesión distintos por día.
Pon el intervalo fijo en un CTE params, guárdala en el proyecto example y devuelve el archivo exacto.
```

Codex debe inspeccionar el proyecto, crear o reutilizar una familia de consultas, guardar una versión
como `sql-projects/example/sql-workspace/temporary/daily-active-users/v001.sql`, ejecutar un receipt
y devolver la ruta absoluta. La ejecución en la base de datos se informa por separado y nunca se supone.

La ejecución automática es opcional. El proyecto registra el nombre del entorno y la conexión real se
guarda en `.sql-engineering/connections.local.json`, ignorado por Git. Si no hay controlador, CLI,
secreto o perfil de conexión, el Skill devuelve `manual_required` y la ruta SQL exacta, y pide al usuario
que ejecute la consulta y devuelva el resultado. Nunca controla Chrome ni una consola web de DA.

## Solicitudes habituales

| Objetivo | Ejemplo de solicitud |
|---|---|
| Crear un proyecto | `$sql-engineering Crea el proyecto alpha para StarRocks e indica qué telemetría, conocimiento, reglas y conexión faltan.` |
| Registrar telemetría | `$sql-engineering Registra este XML sin cambios como definición original de PlayerLogin.` |
| Registrar evidencia de diseño | `$sql-engineering Guarda este libro de configuración de modos como entrada de diseño, no como regla confirmada.` |
| Fijar una regla | `$sql-engineering Fija la definición de usuario activo diario confirmada por una persona como una nueva versión canónica.` |
| Crear SQL | `$sql-engineering Crea y guarda una consulta de usuarios activos diarios para este proyecto.` |
| Modificar SQL externo | `$sql-engineering Importa este SQL, corrígelo para el dialecto del proyecto y no sobrescribas el original.` |
| Buscar trabajo previo | `$sql-engineering Busca consultas guardadas sobre retención y resume su propósito.` |
| Revisar una consulta | `$sql-engineering Añade plataforma como dimensión a la familia existente de usuarios activos.` |
| Conservar una consulta útil | `$sql-engineering Guarda la lógica confirmada como una versión retained.` |
| Verificar la entrega | `$sql-engineering Comprueba el receipt de este v003.sql y devuelve la ruta exacta.` |
| Ejecutar directamente | `$sql-engineering Ejecuta esta consulta guardada en la base de datos de desarrollo configurada.` |

## Cómo funciona el ciclo de vida

```text
solicitud
  -> telemetría original registrada
  -> conocimiento de diseño y confirmado separado
  -> reglas canónicas aplicables fijadas
  -> entorno y dialecto SQL seleccionados
  -> versión SQL temporal guardada
  -> ejecución en el entorno del usuario
  -> corrección o ampliación como nueva versión
  -> versión retained o para dashboard opcional
  -> delivery receipt exacto
```

Una familia de consultas representa una pregunta analítica. Los cambios de fecha, las correcciones
de sintaxis y las ampliaciones que contienen por completo el mismo problema permanecen en esa familia
como nuevas versiones. Un Base, indicador principal o decisión diferente inicia otra familia.

## Estructura del espacio de trabajo

```text
sql-projects/
  _asset_catalog/              extensión reservada para búsqueda entre proyectos
  _review_inbox/               SQL externo y evidencias pendientes de entrada o revisión
  _rule_review/                extensión reservada para revisión de reglas
  example/
    .sql-engineering/
      project.json             identidad y dialecto del proyecto
    sources/
      source-catalog.json
      raw/<source>/vNNN.*      definiciones originales sin modificar
    knowledge/
      planning/<item>/vNNN.*   tablas originales de diseño y configuración
      confirmed/<item>/vNNN.* material confirmado por una persona
    rules/
      definitions/<rule>/vNNN.json
    context/                    notas y manuales no autoritativos
    sql-workspace/
      index.json               índice consultable por máquinas
      temporary/<slug>/
        v001.sql
        v001.meta.json
      retained/<slug>/
      dashboard/<slug>/
```

Los directorios con guion bajo son extensiones entre proyectos. Dentro del proyecto se separan evidencia
original, confirmación humana, reglas canónicas y SQL ejecutable para impedir sustituciones silenciosas.

## Referencia de comandos

| Comando | Propósito |
|---|---|
| `bootstrap` | Crea la estructura del repositorio y, opcionalmente, el primer proyecto |
| `init` | Inicializa un proyecto independiente |
| `environment` | Asocia un entorno del proyecto con un perfil local de conexión a base de datos |
| `source` | Copia y registra una definición original de telemetría sin cambiar su formato |
| `knowledge` | Registra una entrada de diseño o material confirmado por una persona |
| `rule` | Fija una regla confirmada como una nueva versión inmutable |
| `status` | Muestra las fuentes, el conocimiento, las reglas y la ejecución que faltan |
| `save` | Guarda una versión SQL inmutable y actualiza su índice |
| `search` | Busca títulos, resúmenes y etiquetas |
| `receipt` | Verifica una versión SQL exacta antes de entregarla |
| `sql_execute.py run` | Ejecuta una consulta guardada de solo lectura o devuelve una entrega manual |

Prueba la consulta ficticia incluida en
[`daily-active-users.sql`](sql-engineering/assets/examples/daily-active-users.sql).
El [ejemplo completo del agente](sql-engineering/references/example.md) muestra la solicitud, los
comandos, los archivos esperados y el contrato de respuesta final.

## Límites de diseño

- La configuración del proyecto selecciona el dialecto. El Skill no adivina tablas, particiones,
  identificadores de negocio ni definiciones de indicadores.
- El contexto del proyecto es opcional y se declara explícitamente. El Skill no depende de una base de
  conocimiento personal; el esquema ausente puede inspeccionarse con consultas guardadas de solo lectura.
- La ejecución automática utiliza únicamente DB-API o clientes de línea de comandos de base de datos.
  No admite automatización del navegador ni de consolas DA, y vuelve a la ejecución manual sin configuración.
- El SQL externo permanece como entrada inmutable; las revisiones se guardan dentro del proyecto.
- Las versiones guardadas no se sobrescriben. El receipt detecta cambios manuales mediante hashes.
- Una etiqueta de ciclo de vida describe el uso previsto; no demuestra corrección de negocio ni ejecución.
- Resultados, visualizaciones, validaciones y dashboards pueden añadirse mediante extensiones gobernadas,
  pero no se infieren silenciosamente a partir de un archivo SQL.
- No deben confirmarse credenciales, esquemas privados, resultados de producción ni rutas absolutas locales.

## Documentación

| Tema | Documento |
|---|---|
| Flujo del agente y límites obligatorios | [`sql-engineering/SKILL.md`](sql-engineering/SKILL.md) |
| Entradas y flujo de un proyecto nuevo | [`references/project-onboarding.md`](sql-engineering/references/project-onboarding.md) |
| Ejemplo completo | [`references/example.md`](sql-engineering/references/example.md) |
| Contrato del proyecto y directorios | [`references/project-contract.md`](sql-engineering/references/project-contract.md) |
| Ciclo de vida de familias de consultas | [`references/workflow.md`](sql-engineering/references/workflow.md) |
| Comprobaciones de entrega SQL | [`references/sql-quality.md`](sql-engineering/references/sql-quality.md) |
| Entornos y ejecución de base de datos | [`references/database-execution.md`](sql-engineering/references/database-execution.md) |
| Métodos de conexión y dialectos SQL | [`references/dialects.md`](sql-engineering/references/dialects.md) |
| Reglas de contribución | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Política de seguridad | [SECURITY.md](SECURITY.md) |

## Desarrollo

El núcleo público utiliza únicamente la biblioteca estándar de Python. La ejecución DB-API importa
el controlador elegido en el perfil de conexión local del usuario.

```powershell
python -m unittest discover -s .\sql-engineering\tests -p "test_*.py"
python -m py_compile .\sql-engineering\scripts\sql_workspace.py .\sql-engineering\scripts\sql_execute.py
```

Distribuido bajo la [licencia Apache 2.0](LICENSE).
