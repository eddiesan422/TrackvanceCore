# Especificación técnica

Esta carpeta versiona la **Especificación Técnica v1.1** y su fuente editable para
la evolución funcional local `0.8.0`. El nombre v1.1 identifica el documento;
0.8.0 identifica el software. IMPLEMENTADO, PROBADO, PREPARADO y OBJETIVO se distinguen
en el texto; la productización permanece fuera del roadmap local 1–10.

- `Trackvance_Core_Especificacion_Tecnica_v1.1.md`: fuente editable oficial.
- `Trackvance_Core_Especificacion_Tecnica_v1.1.pdf`: documento publicado tras
  generar candidato y revisar renderizado.
- `build_specification.py`: generador portable reportlab/pypdf, separado de las
  dependencias de ejecución del producto.
- `validation_results_0.8.0.json`: resultados y gates de la revisión vigente;
  un resultado pendiente, fallido u omitido no se presume aprobado.
- `corrections_results_0.8.0.json`: presentación explícita de los antecedentes
  C01–C06, sin atribuirlos a la certificación actual de Catálogo/Reportes.
- `parameters_0.8.0.json` y `volume_results_0.8.0.json`: cotas y mediciones vigentes,
  diferenciadas de volúmenes certificados y antecedentes.
- `model_contract_0.8.0.json` y `permission_contract_0.8.0.json`: inventario vigente
  exportado del runtime, con 55 tablas y permisos/rutas de Catálogo y Reportes.
- `validation_results_0.7.0.json`: resultados históricos de la publicación inicial.
- `corrections_results_0.7.0.json`: evidencia histórica propia del ciclo C01–C06;
  un archivo o gate pendiente no se presume aprobado y un resultado inicial no lo sustituye.
- `parameters_0.7.0.json` y `volume_results_0.7.0.json`: cotas y mediciones históricas.
- `model_contract_0.7.0.json` y `permission_contract_0.7.0.json`: inventario histórico completo
  de campos, relaciones, restricciones, índices y permisos exportado del código.
- `validation_results_0.6.1.json`: resultados históricos 0.6.1, preservados.
- `validation_results_0.6.0.json`: resultados históricos 0.6.0, preservados.
- `validation_results_0.5.1.json`: resultados históricos 0.5.1, preservados.
- `validation_results_0.5.0.json`: resultados históricos 0.5.0, preservados.
- `validation_results_0.4.1.json`: evidencia histórica de la entrega anterior.
- `validation_results_0.4.0.json`: evidencia histórica de la entrega anterior.
- `validation_results_0.3.0.json`: evidencia histórica de la entrega anterior.

## Generación y publicación

La implementación 0.8.0 documenta Catálogo de gobierno, clasificación controlada
opcional, glosario, aprobación estricta verificable y Reportes multifuente.
PREVIEW y DOWNLOAD son efímeros; DATASET publica deliberadamente un activo completo
con contexto congelado, linaje y autorización de todas sus fuentes. Hay cuatro
lanes: DEFAULT, ACQUISITION, DELIVERY y REPORT. La migración aditiva 0017 mantiene
las migraciones 0001–0016, amplía el modelo a 55 tablas y el estado nativo a 8.
El antecedente 0.7.0 conserva adquisición durable, Parquet multipart, PySpark,
Delivery, programación, outbox y las correcciones C01–C06 sobre 0016/state 7;
su selector de área libre queda sustituido por gobierno controlado en 0.8.0.
El generador admite índice y marcadores de dos niveles, diagramas de arquitectura,
secuencia y estados, y bloques JSON paginables. La salida es determinista con los mismos inputs;
permite contrastar el hash del candidato revisado con el PDF publicado.
Mientras el contenido cambia se usa `--draft`. El candidato definitivo exige
revisión visual completa y conserva explícitos los gates pendientes al corte.
El CI del commit que incluye ese PDF y la actualización habitual se verifican
después y se registran externamente; publicar el documento no cierra esos gates.

En un entorno de autoría con Python, reportlab y pypdf, desde el repositorio:

```sh
python docs/specification/build_specification.py --draft --results docs/specification/validation_results_0.8.0.json
```

El generador usa esta fuente, `backend/openapi.json`, el logo del repositorio y
los inventarios de modelos/permisos, parámetros, volumen y resultados explícitos
de 0.8.0, incluido `corrections_results_0.8.0.json`. Los insumos versionados 0.7.0
y sus manifiestos se conservan íntegros como antecedentes. Al publicar, el manifiesto
de autoría 0.8.0 debe registrar por separado las identidades y SHA de los insumos
usados, incluidos fuente, generador, OpenAPI, logo y recursos tipográficos,
sin sustituir los históricos.
Reconoce el repositorio desde su ubicación;
`--repo`, `--source`, `--results` y `--output-dir` permiten rutas distintas.
Usa Arial en Windows y DejaVu/Vera cuando Arial no está disponible. No descarga
fuentes ni librerías durante la generación.

El candidato queda en `tmp/pdfs/specification-candidate.pdf`. Revisa las páginas
renderizadas, tablas, encabezados, índice y diagramas antes de publicar con:

```sh
python docs/specification/build_specification.py --results docs/specification/validation_results_0.8.0.json --publish
```

`--draft` marca un candidato cuya certificación sigue en curso e impide combinarlo
con `--publish`. El JSON acepta etiquetas y resultados textuales reales, incluyendo
fallos o pruebas no ejecutadas. Publicar no convierte esos resultados en éxitos.

La copia oficial de esta instalación se encuentra en `ProductOne/Documentación`.
Fuente, generador y PDF publicados deben coincidir en ambas ubicaciones. La
edición original del 10 de septiembre (52 páginas) permanece archivada fuera
de esta carpeta; su SHA-256 es
`82341b3c63710abd996476e1ac9ca453010dcf7918cb3ed7de5d75c4b8b90244`.
Cuando ese archivo archivado está junto al generador, su hash se valida sin
sobrescribirlo. La especificación actual no altera ese antecedente.

La certificación vigente y el inventario de cambios están en
[validación](../development/validation.md), la
[guía CI](../development/ci.md), la
[guía de Catálogo](../development/catalog-governance-0.8.0.md),
[Reportes](../development/reports-0.8.0.md) y la
[guía de uso](../development/catalog-reports-user-guide.md).
Los ADR [0026](../adr/0026-controlled-governance-strict-approval.md),
[0027](../adr/0027-reports-frozen-context-sandbox.md) y
[0028](../adr/0028-isolated-certification-recovery-upgrade-080.md) documentan
gobierno, contexto/ejecutor y compatibilidad 0.8.0.
La [adquisición/volumen 0.7.0](../development/acquisition-volume-0.7.0.md),
[Spark/paridad 0.7.0](../development/spark-volume-0.7.0.md), los ADR 0020–0024
y el [ADR 0025 del ciclo C01–C06](../adr/0025-corrections-c01-c06.md) son antecedentes.
El [informe A-M 0.5.1](../development/release-report-0.5.1.md) permanece histórico.

## Publicación 0.8.0 — PDF revisado; cierre externo pendiente

El PDF vigente contiene **143 páginas, 280 marcadores y 40 secciones principales**,
con texto seleccionable. Sus 143 páginas se renderizaron a 110 dpi y se revisaron
en cuatro rangos completos, sin hallazgos pendientes. La publicación copió los bytes
del candidato aprobado, sin regenerarlo después de la revisión. Su SHA-256 es
`223175c92b9a13a031a9eea99e8cdba671e9fbda2461edede2cd3f032f15055c`.
PDF, fuente, generador, insumos y extracción coinciden byte a byte con las copias
oficiales en `ProductOne/Documentación`; los once archivos de la edición corregida
0.7.0 y el original de septiembre permanecen archivados con sus hashes intactos.
La [verificación PDF](../development/evidence/0.8.0/pdf-verification.json) registra
los hashes de los insumos y de los 143 renders, los cuatro revisores y el corte
de implementación `000e1bc1db497ea56ad5ddef19d454428a847fb8`.

Los oráculos completos de volumen, recuperación nativa y desde las baselines auténticas
tienen evidencias locales con sus cortes explícitos. Los dieciséis jobs GitHub Actions
del SHA documental final y la actualización autorizada de la instalación habitual
siguen siendo gates independientes, registrados externamente después de este commit
para evitar una referencia circular. Las pruebas locales, los jobs parciales o la
publicación del PDF no cierran esos gates. `validation_results_0.8.0.json` y el informe
de validación conservan el estado real al corte; este README no anticipa su aprobación.

## Publicación C01–C06 de 0.7.0 — histórica

El corte de implementación evaluado por el primer formal es
`393b7e25e413bf641d5483c25c53951642611f51`. Su informe conserva el FAIL global de
navegador y los resultados parciales; las correcciones posteriores del harness,
sus repeticiones y el formal final tienen evidencias y revisiones propias.
La evidencia histórica se conserva en `corrections_results_0.7.0.json`
y en los antecedentes de [validación](../development/validation.md). El ciclo usó 0016/state 7, conserva las
42 tablas y no hereda los resultados de la publicación inicial.

El PDF histórico corregido se publicó con **120 páginas, 246 marcadores y 38 secciones
principales**, SHA-256 `493c0d37fd087c70d667832d896b90ade4202d618e99874e73b23bb90812b476`.
Las 120 páginas se renderizaron a 120 dpi y se inspeccionaron visualmente en una
revisión fresca completa, sin hallazgos pendientes. Una segunda generación con
los once inputs congelados, runtime y fuentes idénticos produjo los mismos bytes.
La [prueba PDF](../development/evidence/0.7.0-corrections-20261004/pdf-verification.json)
registra hashes de inputs/extracción/render y los cuatro revisores. El renderizador
advirtió aliases Symbol/ArialUnicode; los recursos Arial usados están embebidos y
los glifos, flechas y acentos fueron inspeccionados legibles.

PDF, fuente, generador, once inputs y extracción tienen copias oficiales byte
idénticas en `ProductOne/Documentación`. El archivo de la publicación inicial
conserva sus doce archivos verificados y el SHA de su manifiesto. La prueba
histórica `Trackvance_Core_PDF_Verificacion_0.7.0.json` sigue intacta. Los datos
históricos siguientes no corresponden al PDF corregido. CI del HEAD final y el
upgrade principal mantienen sus gates externos hasta su ejecución efectiva.
El informe externo de cierre identificará el HEAD documental final, todos los
jobs de su CI y el upgrade efectivo, sin crear una referencia circular en el PDF.

## Publicación inicial 0.7.0 — histórica

La edición publicada contiene **114 páginas, 238 marcadores y 38 secciones
principales**, con texto seleccionable. Su SHA-256 es
`3f229e2863e0115631a3bfe08526c57c6a45240497acc4a30621d71836aa99e5`.
Una segunda generación con los mismos inputs produjo exactamente esos bytes.
Las 114 páginas se revisaron visualmente a 150 dpi y no quedan hallazgos.
La [prueba de publicación](../development/evidence/0.7.0/pdf-verification.json)
registra hashes de fuentes, generador, inputs, fuentes tipográficas, extracción,
renders y copias oficiales. La edición 0.6.1 quedó archivada antes de sustituirla.

El corte de implementación conocido por el documento es
`a5f12ddc2850dea4053ad77121835e42a37a72cf`. El SHA documental final, sus resultados
CI y la actualización efectiva de la instalación principal se registran después
de ese corte en el informe externo de cierre, evitando un hash autorreferente.
La publicación del PDF por sí sola no declara cerrado ninguno de esos gates.

## Publicación histórica 0.6.1

El producto `4d3c656ab0bc250f50eb53972d22839da9e4485c` completó nueve jobs SUCCESS
del [workflow 36327154050](https://github.com/eddiesan422/TrackvanceCore/actions/runs/36327154050).
El primer cierre documental `afb8f8d` falló en la preparación del PostgreSQL
externo del runner de recovery, antes de iniciar Trackvance. Se preservó el
resultado, se corrigió la condición prematura de healthcheck y se repitieron
pruebas. La causa exacta de aquel fallo no es recuperable porque el diagnóstico
original suprimió stderr; no se declara demostrada por la reproducción posterior.

La edición revisada tiene **82 páginas, 179 marcadores y 38 secciones
principales**. Todas las páginas se renderizaron a 110 dpi y se inspeccionaron
antes de publicar. Incluye resultados posteriores a la corrección del runner.
SHA-256 del candidato aprobado y publicado: `3d60da10b31e12bcbf93562405b1850606a6f0c5aa22b4bf0c07cb6941e55af6`.
`--publish` produjo exactamente los mismos bytes. Fuente, generador, resultados,
PDF y extracción coinciden con la copia oficial en `ProductOne/Documentación`.
Las ediciones original, 0.5.1 y 0.6.0 permanecen archivadas e intactas.
[Registro de publicación](../development/evidence/0.6.1/pdf-verification.json)
conserva hashes, OpenAPI, procedencia, revisión visual y avisos de render.

El informe externo registra el HEAD corregido y los nueve SUCCESS observados
antes de la entrega, sin autorreferencia circular en el PDF. Microsoft/Google
reales permanecen NOT_RUN_EXTERNAL_CREDENTIALS; SMTP fue retirado del producto.

## Publicación histórica 0.6.0

El producto y su harness `35c88fa2f0fd0c59dacda7faf76713e064364d25` completaron
los nueve jobs SUCCESS del workflow [36289364326](https://github.com/eddiesan422/TrackvanceCore/actions/runs/36289364326), intento 1.
La edición tiene **77 páginas, 151 marcadores y 37 secciones principales**.
Todas las páginas se renderizaron a 110 dpi y se inspeccionaron antes de publicar:
portada, índice, diagramas, tablas, JSON/código, márgenes, headers/footers y saltos.
Se corrigieron dos negritas en la página 69; los otros 76 PNG permanecieron
idénticos y la página corregida se volvió a revisar. Sin páginas vacías, texto
fuera de página, clipping ni solapamientos observados.

SHA-256 candidato revisado y PDF publicado: `3f72c85c90c3d50661373ec46167adf0b87fdbe13b2d61fc8889257b8542bc6a`.
La publicación mediante `--publish` produjo exactamente los mismos bytes que el
candidato aprobado. Fuente, generador, resultados, PDF y texto extraído coinciden
por hash con la copia oficial `ProductOne/Documentación`; las ediciones originales
y 0.5.1 permanecen archivadas e intactas. El registro completo de hashes,
procedencia, revisión y avisos de render está en
[pdf-verification.json](../development/evidence/0.6.0/pdf-verification.json).

Las pruebas externas Microsoft/Google/SMTP conservan NOT_RUN_EXTERNAL_CREDENTIALS;
OIDC mock y Mailpit no las convierten en PASS. El HEAD documental se certifica de
nuevo antes de entregar; el informe final identifica su SHA y workflow inmutables
sin crear una referencia circular dentro del propio PDF.

## Publicación histórica 0.5.1

El código `8927ea00703ff090708c302d7755dae20a7687b9` obtuvo ocho jobs SUCCESS en
[GitHub Actions 36194431770](https://github.com/eddiesan422/TrackvanceCore/actions/runs/36194431770).
La especificación publicada tiene **52 páginas, 111 marcadores y 27 secciones
principales**. Todas las páginas fueron renderizadas a 100 dpi e inspeccionadas
visualmente antes de `--publish`: portada, índice, diagramas, tablas, JSON,
márgenes, títulos, footers y saltos. Se corrigió el redondeo gzip de la página 31;
los otros 51 PNG permanecieron idénticos y la página cambiada se volvió a revisar.
Los 52 PNG del publicado coinciden exactamente con el candidato aprobado.

SHA-256 publicado: `c69609f26e117b60ea4fe77d893879a064d2c0c242ca1bd8941d9274e34e2813`.
Fuente, generador, resultados, PDF y texto extraído coinciden por hash con la copia
oficial en `ProductOne/Documentación`; la edición 0.5.0 se conservó en Archivo.
La evidencia reproducible está en [pdf-verification.json](../development/evidence/0.5.1/pdf-verification.json).
El commit documental posterior se verifica por CI antes de la entrega final,
sin incorporar su propio SHA dentro del PDF ni crear una referencia circular.

## Publicación histórica 0.5.0

La publicación 0.5.0 incorpora
[GitHub Actions: siete jobs SUCCESS](https://github.com/eddiesan422/TrackvanceCore/actions/runs/35875426350)
sobre `57c0319ae2f0ef27ae63eced8e354258efc0854d`, 869 pruebas Python,
128 Vitest y 25 escenarios Playwright distintos con al menos un PASS.

Se generó el candidato, se renderizaron y revisaron visualmente sus 36 páginas
y se publicó mediante `--publish`. Los 36 PNG del PDF publicado coinciden
exactamente con los del candidato revisado; conserva 26 marcadores. El generador
respeta párrafos Markdown, evita viudas y mantiene enteros los identificadores
de variables. Fuente, generador, resultados y PDF coinciden con la copia oficial.
SHA-256 del PDF publicado:
`d49386c6dbebb59bc33f8d18e35b37ef53b48d346416c85d70b75e389a193460`.

La edición anterior 0.4.1 (33 páginas, 25 marcadores) conserva como antecedente
el SHA-256 `84950486bb046c97c8184a88c1bc6a2ba1967962a52b4643acf9b349134d57ac`;
está recuperable en Git y archivada junto a la copia oficial.
