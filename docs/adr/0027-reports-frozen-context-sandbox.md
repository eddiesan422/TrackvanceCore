# ADR 0027 — Reportes: contexto conjunto, SQL controlado y ejecución aislada

Fecha: 5 de octubre de 2026. Estado: implementada en el árbol de trabajo 0.8.0.
La decisión describe el código; la certificación final, CI y promoción se registran
separadamente en [ADR0028](0028-isolated-certification-recovery-upgrade-080.md).

## Problema y decisión

Un reporte combina salidas estrictamente aprobadas de varios Intake. Resolver cada
fuente al ejecutar cada acción permite mezclar selecciones de momentos distintos.
Un SELECT puede acceder a archivos, extensiones o red; limitar su texto no protege
el proceso. Exportar XLSX mediante librerías que almacenan el libro completo o
archivos temporales tampoco cumple la ejecución efímera.

Se mantienen el monolito modular, PostgreSQL, RBAC, StorageProvider, ArtifactLink
y JobQueue. Reportes tiene entidades propias: ReportDefinition, ReportRevision,
ReportContext y ReportExecution. El Run existente conserva su entrada y configuración
obligatorias. Las fuentes siempre son versiones OUTPUT de Intake, vinculadas a
su entrada, revisión y Run aprobatorio reales. El identificador visible de la
salida no sustituye esas relaciones.

## Resolución y autorización

`resolve` abre una sesión independiente con REPEATABLE READ en PostgreSQL antes
de la primera consulta; todos los permisos, contratos, versiones, aprobaciones,
esquemas y clasificaciones se seleccionan dentro del mismo snapshot. SQLite usa
SERIALIZABLE para pruebas. La transacción corta persiste y confirma el contexto;
no calcula hashes ni ejecuta el reporte bajo bloqueos de fila.

LATEST_APPROVED ordena primero por ordinal de versión INPUT descendente y después
por finalización/identidad del Run. Reprocesar una entrada antigua no la convierte
en la entrada más reciente. Sólo participan revisiones explícitamente admitidas
de una misma identidad de contrato. SPECIFIC fija la versión INPUT. Una vez
seleccionada la aprobación estricta, la inhabilitación vigente, corrupción o cambio
de esquema de esa salida falla: no se busca una salida anterior para ocultarlo.

El contexto dura quince minutos, pertenece a organización y usuario, congela IDs,
hashes, esquema, query compilada, parámetros y revisión opcional, y tiene digest de
integridad. Contiene metadatos protegidos y nunca filas. Cambiar sólo valores de
parámetros declarados puede usar la misma revisión; cambiar estructura, nombre o
tipo de parámetro requiere otra revisión. Las columnas utilizadas deben mantener
su tipo; una columna nueva no utilizada no rompe la definición.

Resolver exige `reports:read`. PREVIEW, DOWNLOAD y DATASET exigen respectivamente
`reports:preview`, `reports:download` y `reports:generate`; guardar revisiones exige
`reports:write`. La autorización transitive y bloqueos vigentes se comprueban antes
de iniciar, durante consumo y antes de publicar. El contexto no otorga un permiso
duradero. El trabajo ya admitido puede finalizar después de expirar el contexto,
con las mismas fuentes y las comprobaciones actuales. La verificación completa de
original, canónico multipart y RUN_MANIFEST se ejecuta fuera del fence y compara
identidades, configuración almacenada/efectiva, métricas y evidencia estricta.

## Consulta y cardinalidad

SQLGlot analiza el AST completo con dialecto DuckDB. Se admite un único SELECT
plano sobre exactamente los alias congelados, proyección explícita, filtros,
agrupación, HAVING y orden. COUNT(*) es la única excepción a la prohibición de `*`.
CTE, subconsultas, UNION, DDL/DML, tablas de sistema, funciones de archivos/red,
extensiones, fuentes adicionales, aleatoriedad y reloj se rechazan. Los nombres
se citan como identificadores y los valores se enlazan como parámetros tipados.
El constructor guiado compila y pasa por la misma política.

Cada JOIN debe introducir una fuente y vincularla por igualdades AND a fuentes
anteriores. Se admiten INNER, LEFT, RIGHT y FULL, con una a dieciséis parejas de
columnas y tipos lógicos iguales. Los filtros previos se aplican a su fuente; el
filtro posterior conserva semántica WHERE, incluidos los nulos de outer joins.
No hay trim, mayúsculas, conversión implícita de llaves ni equiparación de null y
cadena vacía. Identificadores STRING conservan ceros iniciales y Unicode.

Antes del resultado se agrupan las llaves de la población completa filtrada y de
cada prefijo del JOIN. Se calculan multiplicidades, llaves N:M, filas coincidentes,
no coincidentes, cardinalidad observada y expansión. Declarar 1:1 no modifica los
datos: una discrepancia es visible, y N:M real necesita autorización explícita.
Los límites de filas/expansión siguen vigentes cuando N:M está autorizado. PREVIEW
no convierte diez filas de muestra en prueba de cardinalidad de toda la fuente.

El orden incorpora posiciones verificadas de las fuentes como desempate; resultados
agrupados o DISTINCT ordenan por valores de salida. Una consulta repetida sobre las
mismas fuentes tiene orden estable. El registro de una salida derivada identifica
su propia posición; no pretende ser el número original de varias fuentes.

## Aislamiento real

El coordinador inicia un intérprete independiente mediante pipes limitados,
`-I -B`, sin shell, con descriptores ajenos cerrados y una lista mínima de variables.
No hereda conexiones, DATABASE_URL, tokens, credenciales ni configuración secreta.
La entrada técnica tiene máximo 2 MiB; el canal NDJSON usa lotes finitos y cola de
un mensaje para contrapresión. Decimal/fecha/hora usan etiquetas exactas; float
no se serializa como un resultado aparentemente preciso.

El hijo requiere Linux y Landlock ABI >=3. Aplica `no_new_privs`, denegación por
defecto de todos los derechos de archivos del ABI3, permisos READ_FILE sólo para
las partes seleccionadas, lectura de bibliotecas públicas del runtime y una lista
de archivos públicos de CPU/cgroup/zona horaria. No recibe acceso general al
almacén, /tmp, aplicación o /proc: environ queda fuera. Sólo DATASET obtiene
escritura en el directorio spill de su propio intento.

La ruta del cargador de CPython se deriva exclusivamente de `sys.base_prefix/lib`,
también admitida en Landlock junto con `sys.prefix/lib`; esto permite runtimes
relocalizados como GitHub setup-python sin heredar `LD_LIBRARY_PATH` del proceso padre.

libseccomp deniega socket/red, exec, fork/vfork, clone3, namespaces, mount, ptrace,
process_vm, keyring, bpf, io_uring y otros escapes. clone sólo puede crear threads.
El proceso debe tener un único thread al instalar la política; DuckDB se importa
después, porque su conexión por defecto puede crear threads. Así todos heredan el
dominio Landlock incluso en ABI3 sin TSYNC. Se aplican RLIMIT_AS, CPU y descriptores,
core=0 y señal de muerte del padre. Cualquier fallo de instalación termina sin
ejecutar la consulta. Windows no activa una ejecución con menor aislamiento.

DuckDB añade allowlists de paths/directorios, bloqueo de configuración, desactivación
de acceso externo y autoload/autoinstall de extensiones. Estas opciones complementan
el aislamiento del sistema operativo; no lo sustituyen. No se necesita contenedor
privilegiado, SYS_ADMIN ni habilitar user namespaces que Docker bloquea por defecto.
Las pruebas reales utilizan cap-drop ALL, no-new-privileges y network none.

La política se apoya en la documentación primaria de [Landlock](https://docs.kernel.org/userspace-api/landlock.html),
[seccomp](https://man7.org/linux/man-pages/man2/seccomp.2.html) y
[seguridad DuckDB](https://duckdb.org/docs/stable/operations_manual/securing_duckdb/overview).
La restricción Landlock sólo protege los accesos que su ABI soporta; seccomp y el
proceso sin secretos cubren red/escapes que ABI3 no filtra por sí solo.

## Perfiles y publicaciones

PREVIEW devuelve hasta diez filas del resultado real, con límite duro independiente
de variables. DOWNLOAD produce CSV o XLSX incremental sin spill, spool, archivo
intermedio, disco ni tmpfs. ZIP utiliza data descriptors sobre un stream sin seek.
Decimal y enteros que Excel no representa exactamente son texto; null es celda
ausente y cadena vacía es inlineStr explícita. Texto nunca se crea como fórmula.
CSV usa un contrato reversible de null y prefijos documentado en la guía.

Los perfiles tienen presupuestos finitos de tiempo, filas, bytes, memoria y lotes.
La admisión usa una exclusión transaccional PostgreSQL y conteo global de ejecuciones
RUNNING: API y workers comparten concurrencia, además de límites locales de proceso.
Los límites del contenedor constituyen un límite adicional; RLIMIT_AS no garantiza
la disponibilidad de toda esa memoria. Un resultado que excede un límite falla,
sin truncarse para declarar éxito. La API puede proponer DATASET ante un límite
efímero, y conserva los errores de permiso, SQL o cardinalidad.

DATASET usa la lane REPORT de JobQueue, lease, heartbeat, intentos máximos y
recuperación. Prepara partes canónicas String/null en
`report-staging/<execution>/<attempt>-<owner>`; contabiliza staging y spill y
perfila íntegramente la salida fuera del fence. Promueve artefactos inmutables y
verifica partes antes de un fence corto con orden de bloqueo Execution→Job y CAS
de lease vigente. Dataset, versión, gobierno, dependencias de seguridad, linaje,
metadatos de ejecución y SUCCESS del Job se confirman juntos. No existe una versión
parcial visible. La clave de idempotencia identifica usuario/organización y payload;
repetirla devuelve la misma ejecución, incluso tras perder la respuesta.

La nueva versión REPORT_OUTPUT es READY y PENDING_VALIDATION, versión 1 de un
dataset nuevo. Hereda la clasificación más restrictiva y dependencias sobre todas
las fuentes. Necesita su propia aprobación Intake antes de servir a otro reporte.
Cada fuente se conserva en ArtifactLink junto a Run aprobatorio, revisión, versión
de salida y artefacto. Ninguna dependencia se borra para eludir una revocación.

Un worker antiguo no puede publicar después de perder su lease. Los candidatos
de su intento sin Artifact confirmado se reclaman por IDs deterministas; el
cleanup nunca elimina artefactos referenciados. Los backups excluyen sólo staging,
no los registros o artefactos permanentes. Recuperación y restore verifican el
contexto, referencias multipart, linaje y dependencias de seguridad.

## Evidencia y límites de interpretación

Las suites Reports cubren AST hostil, oráculo independiente de joins, población
completa, N:M, tipos exactos, CSV/XLSX, denegación durante la ejecución y recorrido
Intake→reporte→dataset→nuevo Intake. Además, el verificador PostgreSQL aislado
publica dos Intake reales en otra conexión entre las dos lecturas de fuentes;
el contexto REPEATABLE READ conserva [1,1] y el siguiente selecciona [2,2],
sin mezclar aprobaciones y eliminando su esquema/almacén UUID propios.
La imagen de diagnóstico con strace observa
desde el inicio de la consulta al coordinador, exportador y descendientes; exige
ausencia de aperturas de archivos regulares en modo escritura, mutaciones o mmap
de archivo compartido escribible. El descriptor stderr heredado hacia /dev/null
es un dispositivo de descarte y no almacena resultados. La inicialización del
backend y escritura de auditoría/metadata no son almacenamiento de resultados.

`max_rss_bytes` mide máximo RSS del hijo DuckDB, no la suma simultánea de API,
exportador, Postgres y worker. `elapsed_seconds` es tiempo del motor después de
conectar; el coordinador mantiene su deadline global. Staging observado es un
conteo muestreado durante DATASET, no un pico exacto de disco. PREVIEW indica
sample y total desconocido; éxito de transmisión ASGI acredita envío al cliente,
sin afirmar que el navegador guardó un archivo. Certificación de volumen y
recursos del entorno requiere la evidencia adicional de ADR0028.
