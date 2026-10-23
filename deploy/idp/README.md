# IDP local 0.9.8

Esta identidad GPU es independiente de `trackvance-codex` y de la instalación
habitual. No montar sus volúmenes, bases de datos, credenciales ni archivos `.env`.
La autorización del usuario permite modelos oficiales; nunca usar AWS real.

## Preparación reproducible

1. Ejecutar `python scripts/idp_models.py --destination <cache-privada>` fuera del
   repositorio. El lock fija revisiones y SHA-256; el reporte `acquisition.json`
   registra cada archivo. El script sólo descarga de PaddlePaddle en Hugging Face
   y del registro oficial de Ollama. No ejecuta Python remoto de los modelos.
2. Definir `IDP_MODEL_ROOT=<cache-privada>` y ejecutar
   `docker compose -f compose.idp-local.yml up -d`. Las imágenes están fijadas por
   digest. Se mantiene seccomp por defecto, usuario 65532, sistema raíz de sólo
   lectura, capabilities vacías y `no-new-privileges`. Únicamente `/tmp` y el
   índice efímero `manifests-v2` de Ollama permiten escrituras.
3. Consultar `http://127.0.0.1:3288/health`. Distingue modelos instalados,
   importación de Paddle y un kernel CUDA pequeño real, e inferencias de modelos
   observadas por este gateway. La disponibilidad exige runtime y hashes válidos.
   Estos diagnósticos no acreditan exactitud sobre el corpus ni aceptación final.
4. El proceso coordinador documental utiliza `TRACKVANCE_IDP_LOCAL_URL`, con
   origen permitido `http://127.0.0.1:3288`, `http://host.docker.internal:3288`,
   `http://idp-ingress:8088` o `http://idp-gateway:8088` en una red
   interna explícitamente conectada. Docker Desktop puede acceder al relay por
   `host.docker.internal` desde su red ordinaria, sin conectar la red GPU a la
   base de datos. No cambiar el sandbox del hijo OCR.

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
timeout efectivo admite 30–1800 segundos; no se garantiza throughput.
`use_llm=false` mide Paddle solo y `use_llm=true` añade interpretación semántica.
La ruta digital suficiente registra `inference_skipped=true`.
La extracción por reglas se ejecuta antes del LLM; si ya es suficiente, Qwen se
omite también después de Paddle. `max_retries` limita a cero, uno o dos intentos
focalizados de hasta cuatro regiones. Esos recortes usan spotting y conservan
los polígonos que realmente produce el modelo. Los resultados previos y los
desacuerdos permanecen trazables y requieren resolución explícita.

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
No se descartan filas repetidas por compartir rectángulo. El LLM sólo propone
subcadenas presentes en observaciones referenciadas. Los tipos y cantidades se
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
