# IDP local 0.9.8

Esta identidad GPU es independiente de `trackvance-codex` y de la instalación
habitual. No montar sus volúmenes, bases de datos, credenciales ni archivos `.env`.
La autorización del usuario permite modelos oficiales; nunca usar AWS real.

## Preparación reproducible

1. Ejecutar `python scripts/idp_models.py --destination <cache-privada>` fuera del
   repositorio. El lock fija revisiones y SHA-256; el reporte `acquisition.json`
   registra cada archivo. El script sólo descarga de PaddlePaddle en Hugging Face
   y del registro oficial de Ollama. No ejecuta Python remoto de los modelos.
2. Ejecutar `python scripts/idp_runtime.py --cache <runtime-privado> --models
   <cache-privada>`. El instalador descarga la rueda oficial cu129 3.2.1 y verifica
   su SHA-256; `requirements.lock` fija con hashes las 114 dependencias. Usa
   `Dockerfile.software` y un builder propio limitado a 3 GiB/dos CPU. Exporta
   el software a un archivo privado, sin importar sus grandes capas al daemon.
   Carga sólo `Dockerfile.gateway`, basado en Python 3.12.15 por digest, e
   inicializa un volumen nuevo con un contenedor sin red ni GPU, usuario 65532,
   512 MiB de RAM/swap y una CPU. Verifica los SHA-256 de ambas capas antes de
   extraerlas. El builder propio se elimina al terminar; no se limpian cachés
   globales ni recursos de Codex. `runtime-receipt.json` registra imagen local
   inmutable, etiquetas propias, capas, rueda, lock y archivos fuente.
3. Ejecutar `docker compose --env-file <runtime-privado>/runtime.env -f
   compose.idp-local.yml up -d`. El compose exige la identidad local verificada
   y monta el volumen de software sólo lectura. El código del gateway está
   incluido en la imagen inmutable. Se mantiene seccomp por defecto, usuario 65532, sistema raíz de sólo
   lectura, capabilities vacías y `no-new-privileges`. Únicamente `/tmp` y el
   índice efímero `manifests-v2` de Ollama permiten escrituras.
4. Consultar `http://127.0.0.1:3288/health`. Distingue modelos instalados,
   importación de Paddle y un kernel CUDA pequeño real, e inferencias de modelos
   observadas por este gateway. La disponibilidad exige runtime y hashes válidos.
   Estos diagnósticos no acreditan exactitud sobre el corpus ni aceptación final.
5. El proceso coordinador documental utiliza `TRACKVANCE_IDP_LOCAL_URL`, con
   origen permitido `http://127.0.0.1:3288`, `http://host.docker.internal:3288`,
   `http://idp-ingress:8088` o `http://idp-gateway:8088` en una red
   interna explícitamente conectada. Docker Desktop puede acceder al relay por
   `host.docker.internal` desde su red ordinaria, sin conectar la red GPU a la
   base de datos. No cambiar el sandbox del hijo OCR.

Un archivo de software creado por este procedimiento puede reutilizarse con
`--software-archive <archivo-propio> --software-sha256 <sha-del-recibo>`;
el instalador exige el hash completo y comprueba el lock incrustado y la rueda
oficial en su historial de construcción. No adoptar archivos ajenos. Un volumen
previo exige `--runtime-volume <nombre-propio>` y etiquetas de propietario
coincidentes; su recibo debe acreditar exactamente las dos capas esperadas.
El instalador no inicia automáticamente servicios ni declara inferencia válida.

Para copiar los modelos verificados a almacenamiento ext4 privado, añadir
`--snapshot-models` al mismo comando de preparación. Siempre crea un volumen
nuevo con identidad y finalidad propias; no reutiliza ni sobrescribe volúmenes
de modelos. El helper usa Python base, usuario 65532, sin red ni GPU, raíz de
sólo lectura, 512 MiB y una CPU. Comprueba espacio, una reserva de RAM de 3,25 GiB
en Windows, inventario exacto de archivos/directorios, SHA-256 de origen y
destino y estabilidad del descriptor de origen. El límite es 10 GiB/900 segundos.
El recibo completo se publica atómicamente y vincula volumen, imagen, lock y
archivos; incluye el directorio vacío que requiere el tmpfs de Ollama.
Tras retirar el helper y comprobar que no queda un escritor, genera
`models.ext4.override.json` y `model-snapshot-receipt.json` en la caché privada.
Para usarlo, añadir `-f <runtime-privado>/models.ext4.override.json` al comando
compose del paso 3. Los subdirectorios Paddle/layout/Ollama quedan sólo lectura;
se conservan los originales y todas las verificaciones del gateway y del hijo.
Omitir ese segundo archivo permite volver a los binds originales. El instalador
conserva un volumen incompleto si falla y no realiza limpieza global. Esta
ubicación no acredita una mejora de velocidad: requiere medición separada.

La imagen oficial SM120 se conserva en el manifiesto como referencia fallida:
dos descargas independientes devolvieron capas con SHA-256 distintos de los
esperados. Nunca se ejecutó, no se omitió la verificación y no es el runtime del
compose. La instalación manual sigue la ruta cu129 de la guía oficial Blackwell.

El compose limita ambos servicios a 6 GiB de RAM y tres CPU cada uno, y GPU 0.
La red `inference` es interna: sus intérpretes no tienen salida a Internet. Un
relay HTTP separado, sin GPU, modelos ni credenciales, publica localhost3288 y
reenvía únicamente rutas conocidas al gateway. Esto permite acceso desde Windows
sin dar una ruta externa a Paddle/Qwen. Ollama no se publica. No requiere secretos.

## Contrato y límites

El coordinador comienza con extracción digital dentro del confinamiento
Landlock/seccomp histórico. Si hay escaneados, imágenes embebidas o validaciones
insuficientes, envía bytes acotados al gateway. El gateway procesa un documento
a la vez; los trabajos adicionales esperan dentro de su timeout. Nunca cambia hacia un
proveedor externo. Cada trabajo fija proveedor, modelos, prompt y configuración.

`max_parallel_jobs` limita realmente las solicitudes de inferencia admitidas,
incluyendo activa y cola, entre uno y cuatro. El mínimo configurado por las
solicitudes concurrentes prevalece sobre la GPU compartida; el exceso devuelve
ocupación explícita. La ejecución física GPU continúa limitada a uno, incluso
cuando se admiten cuatro solicitudes.

El proceso Paddle termina antes de iniciar Qwen. Ollama usa un trabajo, contexto
8192 por defecto y `keep_alive=0`, evitando retener ambos modelos en VRAM. El
asignador Paddle crece bajo demanda y limita su proceso a 10.000 MiB de VRAM;
una asignación que exceda ese límite falla en lugar de tomar toda la GPU.
El timeout efectivo admite 30–1800 segundos; no se garantiza throughput.
`use_llm=false` mide Paddle solo y `use_llm=true` añade interpretación semántica.
La ruta digital suficiente registra `inference_skipped=true`.
La extracción por reglas se ejecuta antes del LLM; si ya es suficiente, Qwen se
omite también después de Paddle. Cuando existen literales OCR pero falla su
mapeo, Qwen los interpreta antes de iniciar recortes costosos. Los reintentos
sólo atienden candidatos aún inválidos o en conflicto; la falta de prueba espacial
de cobertura sigue bloqueando la aprobación aunque no justifique más inferencia.
`max_retries` limita a cero, uno o dos intentos focalizados de hasta cuatro regiones.
Se prefieren anclas observadas únicas dentro de los límites declarados; no se usa
una página completa como sustituto de un campo sin localizar. La deduplicación
compara los rectángulos expandidos y evita repetir recortes contenidos. Spotting
conserva sus polígonos reales. Los resultados previos y los desacuerdos permanecen
trazables y requieren resolución explícita.

El contrato semántico `trackvance-semantic-098-v2` conserva nombres, etiquetas,
alias, tipos, formatos numéricos/fechas y el grupo de captura literal declarado.
Omite muestras de configuración, contextos del constructor, UUID y coordenadas
de reglas; la evidencia mantiene texto, página, rectángulo real e índice. Nunca
se recortan silenciosamente observaciones para ajustar el contexto. Ollama recibe
`truncate=false` y `shift=false`; sólo se acepta `done=true`, `done_reason=stop`
y conteos reales verificables. El contexto es de 1024–32768 tokens: la salida
admite `min(8192, contexto/2)` y la entrada el resto. Los conteos finales se
comprueban contra ambos límites; una respuesta incompleta o excesiva falla.
Los bytes UTF-8 registrados no se presentan como tokens. Si el runtime no puede
verificar el presupuesto, no se devuelve un candidato exitoso.

El prompt `trackvance-idp-098-v2` selecciona únicamente identificadores de
fragmentos, sin devolver valores escritos por el modelo. El catálogo común
conserva observación, offsets y subcadena exacta; el cliente lo reconstruye y
comprueba su hash antes de resolver cada selección. Incluye líneas, celdas
separadas por barras o tabuladores, sufijos tras dos puntos y tokens completos,
con límites de 4096 fragmentos y 120.000 bytes UTF-8 de texto; excederlos produce
un error explícito. El prompt incluye cada línea mediante su fragmento original,
sin duplicar las observaciones completas ni eliminar evidencia para reducirlo.
La máscara de trabajo sólo solicita cabeceras inválidas o ausentes y tablas
incompletas o en conflicto. Las cabeceras restantes quedan nulas y las tablas
restantes vacías en la respuesta IA. El prompt v1 permanece disponible para
configuraciones históricas y mantiene su comprobación estricta de subcadenas.

Los errores HTTP usan códigos fijos para contexto, respuesta incompleta,
integridad de modelos, runtime, cancelación, capacidad y timeout. No se transmiten
stderr, rutas ni mensajes libres del modelo. Un fallo posterior a la lectura
inicial conserva en `OperationError.details.local_inference` la etapa fallida y
el candidato/historial para evidencia privada, con límite de ocho MiB; nunca se
califica ese candidato parcial como ejecución correcta.

`scripts/idp_benchmark.py` acepta `--context-tokens` (1024–32768, por defecto 8192)
y `--max-retries` (0–2, por defecto 1). Ambos se propagan a la configuración
congelada de cada ejecución. Cada invocación escribe un reporte exclusivo
`benchmark-matrix-<uuid>.json` y conserva los intentos anteriores.

Límites: original 32 MiB, 50 páginas, 20 millones de píxeles por página,
respuesta 16 MiB, 100.000 observaciones, 10.000 filas por tabla. El gateway acepta
PDF, PNG y JPEG; no acepta rutas, URLs, archivos ejecutables ni modelos libres.
La cancelación termina el proceso Paddle activo y cierra la conexión HTTP de
Qwen registrada antes de esperar cabeceras, también durante la carga inicial.
El coordinador conserva sus controles de cancelación y autoridad de publicación.
Su funcionamiento real sobre GPU debe verificarse con evidencia aparte.

La primera pasada Paddle conserva coordenadas reales de bloques. Las filas de una tabla comparten
el rectángulo del bloque cuando el proveedor no proporciona coordenadas por
celda; los reintentos focalizados conservan las coordenadas de spotting cuando
existen. Jamás se inventan ubicaciones, porcentajes de confianza ni revisión humana.
No se descartan filas repetidas por compartir rectángulo. El LLM sólo selecciona
fragmentos originales o, con el contrato v1, propone subcadenas presentes en
observaciones referenciadas. Los tipos y cantidades se
normalizan mediante el validador de Trackvance. Un conflicto con un valor válido
queda ambiguo. La cobertura de filas requiere controles independientes y no se
acredita por la sola respuesta de Paddle/Qwen.

## Fuentes oficiales verificadas

- [PaddleOCR-VL-1.6](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6), revisión
  `c5630abae1d940eafe0697512a0325494b02ab42`, Apache 2.0.
- [Guía Blackwell](https://github.com/PaddlePaddle/PaddleOCR/blob/dab3fe35379033fdcb2d0e9572fac0b36c9a9ebf/docs/version3.x/pipeline_usage/PaddleOCR-VL-NVIDIA-Blackwell.en.md).
  Requiere driver CUDA 12.9 y rueda cu129; la guía genérica cu126 no es la ruta
  recomendada para SM120. El fabricante verifica RTX 5070; Trackvance debe probar
  la RTX 5070 Ti disponible antes de declarar aceptación.
- [Qwen3.5 9B Q4_K_M](https://ollama.com/library/qwen3.5:9b-q4_K_M), manifiesto
  `56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90`.
  Pesos GGUF 5.629.109.120 bytes, proyector BF16 921.704.832 bytes, Apache 2.0.
- [GPU de Ollama](https://docs.ollama.com/gpu) y
  [capacidad CUDA de NVIDIA](https://developer.nvidia.com/cuda/gpus): RTX 5070 Ti
  tiene capacidad 12.0. Una compatibilidad documentada no sustituye inferencia real.

Las métricas, fallos y aceptación pertenecen a los reportes de ejecución; este
procedimiento no afirma porcentajes de exactitud, estabilidad ni capacidad.


Diagnóstico local opcional: `IDP_STACK_DIAGNOSTICS=1` en el archivo privado de
entorno habilita una única captura de pila a los60s; el valor predeterminado es0.
La solicitud/documento no puede habilitarla. El watchdog se cancela al terminar;
puede alterar tiempos o carreras y no constituye una corrección del bloqueo.
Los marcadores de fase contienen únicamente nombres fijos, tiempos observados,
ordinales de página y hashes de código/fuente, nunca texto documental.

Tras detener y recoger un hijo fallido, el gateway conserva como máximo cuatro
diagnósticos privados en su propio `/tmp/idp-failures-*`: hasta256KiB de stderr y
64KiB de metadatos por registro (1.25MiB total), permisos0700/0600 y TTL de1hora
comprobado al menos cada60s mientras vive el proceso. El stderr puede contener
texto no confiable: nunca se devuelve por HTTP, estado, detalles de error o logs
generales. Exportar esa evidencia privada antes de recrear el contenedor; el
almacenamiento es efímero. Un fallo de diagnóstico conserva el error original.

El runtime empaqueta 15 fuentes verificadas. El gateway captura doce al arrancar,
incluidos `idp_memory_lifecycle.py` e `idp_cache_advice.py`; el hijo Paddle conserva
sus tres fuentes capturadas, incluido `idp_observability.py`. La salud separa
archivos instalados, kernel CUDA y las inferencias realmente observadas.
Kernel PASS no prueba lectura de documentos ni aceptación del corpus. Una
prueba con proceso/tmpfs nuevos no demuestra que estén frías las cachés del
controlador o del sistema; esos estados permanecen sin verificar.


## Presupuesto y cierre de recursos de inferencia

Cada solicitud puede llevar `request_budget_seconds`, una asignación efímera del
presupuesto ya vigente en el coordinador, fuera de la configuración congelada.
El gateway mide un único plazo desde la llegada: recepción, cola, inferencia y
cierre consumen esa asignación. Una reserva interna permite detener y recoger al
hijo Paddle y cerrar y esperar al lector HTTP de Ollama. La conexión conserva su
socket propio incluso si la respuesta HTTP lo separa del objeto de conexión.
La cancelación aceptada durante el cierre impide devolver un candidato exitoso.

Antes de iniciar otra etapa y tras terminarla, se exige ausencia de hijos y
lectores propios, además de observar `/api/ps` vacío. Si queda exactamente el
modelo fijado por nombre y digest, se vuelve a verificar `/api/tags` y se solicita
su descarga de VRAM con una petición vacía y `keep_alive=0`; después se observa
la residencia vacía. La respuesta `done_reason=unload` por sí sola no prueba que
el modelo ya haya salido de memoria. Un propietario desconocido, hash distinto,
lector sin cerrar o estado de residencia no comprobado bloquea ambas rutas de
inferencia con HTTP 503 y salud no disponible; `/status` y `/cancel` permanecen
accesibles. Se conserva privadamente el primer candidato y fallo originales.
La recuperación exige comprobar el cierre antes de recrear el gateway propio.

`IDP_CACHE_HYGIENE=1` habilita un consejo opcional de caché sólo desde el entorno
privado de despliegue; por defecto está deshabilitado. Requiere además un montaje
propio verificado de sólo lectura de la raíz de modelos Ollama en
`/models/ollama`, mediante un overlay privado revisado. El compose predeterminado
no incorpora ese montaje adicional ni activa esta opción. Los modelos y software
deben pertenecer al UID 65532 y conservar las identidades verificadas. Una
configuración incompleta queda bloqueada; no se adopta un volumen ajeno.

Tras comprobar el cierre y la residencia vacía, un hijo sin datos documentales
puede aconsejar DONTNEED sólo en `/opt/venv`, `/models/paddle`, `/models/layout`
y `/models/ollama`: descriptores sin seguir enlaces, mismo dispositivo y dueño,
archivos de un solo enlace, directorios y bytes inmutables. No lee contenidos
para aconsejar ni cambia controles globales o WSL. Su límite de 128 MiB y hasta
4 s pertenece al tiempo restante de la misma solicitud; no abre otro presupuesto.
Los errores ordinarios de consejo son metadatos y no cambian el candidato. Una
identidad de recurso desconocida o un hijo sin recoger bloquea nueva inferencia.
El consejo no garantiza recuperar RAM de Windows ni certificar cachés frías.
