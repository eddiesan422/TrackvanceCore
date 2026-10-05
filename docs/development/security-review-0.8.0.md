# Revisión de gobierno y Reportes 0.8.0

Este documento describe las fronteras implementadas. Los conteos y resultados
ejecutados se registran en [validación](validation.md); una descripción del código
no certifica el volumen, la recuperación ni los jobs del SHA final.

## Gobierno, identidad y acceso transitivo

Macrodominio, dominio y responsables se validan contra la organización actual.
La clasificación es opcional: una carga sin clasificar conserva NULL/UNKNOWN.
Los textos legacy no asignan dominios nuevos. La modificación de metadatos usa
versión esperada y auditoría; no crea versiones de datos ni altera archivos.
Desactivar un término conserva asociaciones históricas, pero impide asociarlo
de nuevo. Los roles personalizados conservan sus grants anteriores; los nuevos
códigos y sus dependencias pertenecen a la autoridad RBAC persistida.

La visibilidad de metadatos del Catálogo se distingue del acceso al contenido.
Vista previa, descarga, perfiles con valores, artefactos de ejecuciones, hallazgos,
exports y consumo por módulos comprueban las fuentes de un derivado. El grafo
incluye dependencias de seguridad activas y relaciones inmutables verificadas.
Ciclos, fuentes ausentes, relaciones entre organizaciones o una cadena demasiado
profunda se rechazan. La herencia histórica se resuelve por IDs de versión,
ejecución y contrato; un nombre de dataset no prueba procedencia ni autorización.

Un bloqueo REPORT restringe el consumo por Reportes; CONTENT restringe el acceso
al contenido, también desde derivados. Liberar una dependencia es una acción
explícita y auditada. Si una nueva ejecución vuelve a consumir esa fuente, la
dependencia se restablece y conserva el historial de su liberación anterior.
Las rutas nativas comprueban la misma política para impedir accesos alternativos.

## Aprobación estricta y publicación

SUCCESS y una decisión global APPROVED no bastan. La elegibilidad exige un
Intake finalizado, no cancelado, con población positiva, entrada procesada por
completo, aceptados y salida íntegra, cero errores, advertencias y descartes,
reglas habilitadas y cobertura efectiva demostrada. La evidencia debe coincidir
con las identidades de entrada, salida y contrato. No se redondea una tasa para
inferir aceptación total ni se crea una aprobación histórica durante migración.

Antes de ejecutar Reportes se verifica además SHA y tamaño de originales,
canónicos, partes, manifest y evidencia, junto con hashes de configuración y
relaciones persistidas. Esa lectura se realiza fuera de los locks y transacciones
largas. Una clasificación posterior puede habilitar consumo actual, pero no
reescribe la decisión ni el snapshot de gobierno de una ejecución pasada.

Intake y Delivery revalidan el ejecutor, sus permisos actuales y las fuentes
antes de publicar o entrar en STARTED. Un retiro de autorización durante el
cálculo impide publicar una versión nueva. El worker de Reportes hace sus propias
comprobaciones de contexto, cupo, estado, lease y permisos antes de publicar.
Un proceso perdido o un contexto manipulado no puede registrar un éxito parcial.

## Consultas y exportación de Reportes

La [guía de Reportes](reports-0.8.0.md) y
[ADR 0027](../adr/0027-reports-frozen-context-sandbox.md) detallan la autoridad SQL,
los parámetros tipados, las versiones congeladas y las cotas. Resolver metadatos
exige reports:read; vista previa, descarga y generación mantienen permisos
independientes y vuelven a validar el contexto en cada acción. Un ID de contexto
o definición no permite leer ejecuciones de otro usuario u organización.

La ejecución usa un proceso Linux hijo con filesystem limitado a fuentes
verificadas, Landlock ABI 3 y seccomp que deniega red y escapes de procesos.
La frontera se aplica antes de importar DuckDB o crear threads. No hay fallback
sin aislamiento si falta el kernel o una biblioteca requerida. La API y los
workers no entregan secretos SQL al ejecutor. La canalización limita lote, celda,
filas, bytes, memoria, tiempo y temporales, y termina el hijo al cancelar o perder
el coordinador. Los límites se comprueban sobre la población procesada; no se
declara éxito truncando el resultado.

Los listados usan COUNT/OFFSET/LIMIT SQL. El catálogo de Reportes filtra la revisión
más reciente y sus fuentes JSON en PostgreSQL y SQLite antes de paginar. El linaje
resuelve nombres y enlaces sólo de entidades visibles; las revisiones enlazan su
ID exacto para que abrir una evidencia histórica no seleccione otra revisión.

## Recuperación y evidencia privada

State 8 conserva las 55 tablas, contextos, dependencias, bloqueos y linaje. Los
artefactos publicados y sus partes se verifican; sólo report-staging se excluye
de la raíz del volumen de datos. Proyectar a state 7 requiere las trece tablas
nuevas vacías y únicamente defaults permitidos, sin clasificación automática ni
reescritura de registros anteriores.

Los drills usan proyectos UUID autorizados, archivos privados y PostgreSQL
sintético. El runtime auténtico 0.7.0 se construye con git archive del commit
fijo, sin cambiar el checkout ni usar volúmenes habituales. Se compara el estado
antes de activar procesos automáticos. Dump, volúmenes, credenciales y logs
diagnósticos quedan privados; sólo resultados saneados se publican. Las guardas
de contexto y sus tests no sustituyen ejecutar y registrar ambos drills.

La frontera de credenciales y los límites de recuperación de secretos descritos
en [security-review-0.6.1.md](security-review-0.6.1.md) siguen siendo antecedentes
aplicables. Un respaldo recupera secretos SQL cifrados y sus claves, pero no la
configuración externa ni los destinos remotos del usuario.
