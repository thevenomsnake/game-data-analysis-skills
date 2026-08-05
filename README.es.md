# Game Data Analysis Skills

**Un ciclo de vida SQL para Codex respaldado por archivos y ejecución de base de datos configurable de solo lectura.**

Game Data Analysis Skills convierte el trabajo SQL de una conversación en archivos de proyecto
duraderos. Cada consulta generada o modificada se guarda, versiona, indexa, se puede buscar y se
entrega mediante una ruta exacta, para que el trabajo siga siendo comprensible cuando termina el chat.

[English](README.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

> Un bloque SQL en el chat es una explicación. El archivo `vNNN.sql` verificado es el entregable.

## Qué problema resuelve

El SQL creado en un chat se pierde con facilidad. Una consulta útil suele copiarse a otro archivo,
editarse sin historial o separarse de la explicación de su propósito. Más adelante, nadie sabe qué
versión produjo un resultado o si se sobrescribió un archivo externo.

Este Skill proporciona a Codex un contrato de espacio de trabajo pequeño y verificable:

| Capacidad | Qué ocurre |
|---|---|
| Inicialización del repositorio | Crea una estructura estable `sql-projects/` y el primer proyecto |
| Entrega de SQL | Guarda cada consulta generada o modificada como una versión inmutable `vNNN.sql` |
| Ejecución por entorno | Ejecuta SQL guardado mediante un controlador DB-API de solo lectura o una CLI de base de datos |
| Entrada de SQL externo | Trata el archivo recibido como entrada y trabaja con una copia dentro del proyecto |
| Historial consultable | Registra título, propósito, etiquetas, dialecto, ruta y hash de contenido |
| Control de revisiones | Mantiene correcciones y ampliaciones en una familia de consultas sin sobrescribir el historial |
| Recibo exacto | Verifica archivo, metadatos, índice y hash actual antes de entregar |
| Etiquetas de ciclo de vida | Distingue SQL temporal, reutilizable y orientado a dashboards |

La edición pública de la especificación no contiene esquemas de empresa, tablas de producción,
credenciales, reglas de negocio privadas, resultados de consultas ni integraciones internas de ejecución.

## Empieza en tres minutos

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

El repositorio ya incluye la estructura compartida de `_asset_catalog`, `_review_inbox` y
`_rule_review`. `bootstrap` repara directorios ausentes e inicializa `sql-projects/example`;
volver a ejecutarlo no elimina contenido existente.

### 3. Pide el trabajo a Codex con lenguaje natural

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
  -> contexto de proyecto y dialecto
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
    sql-workspace/
      index.json               índice consultable por máquinas
      temporary/<slug>/
        v001.sql
        v001.meta.json
      retained/<slug>/
      dashboard/<slug>/
```

Los tres directorios con guion bajo son puntos de extensión estables. El núcleo público los crea,
pero no inventa contenido de catálogo, revisión ni reglas.

## Referencia de comandos

| Comando | Propósito |
|---|---|
| `bootstrap` | Crea la estructura del repositorio y, opcionalmente, el primer proyecto |
| `init` | Inicializa un proyecto independiente |
| `environment` | Asocia un entorno del proyecto con un perfil local de conexión a base de datos |
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
| Ejemplo completo | [`references/example.md`](sql-engineering/references/example.md) |
| Contrato del proyecto y directorios | [`references/project-contract.md`](sql-engineering/references/project-contract.md) |
| Ciclo de vida de familias de consultas | [`references/workflow.md`](sql-engineering/references/workflow.md) |
| Comprobaciones de entrega SQL | [`references/sql-quality.md`](sql-engineering/references/sql-quality.md) |
| Entornos y ejecución de base de datos | [`references/database-execution.md`](sql-engineering/references/database-execution.md) |
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
