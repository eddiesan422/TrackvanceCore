# Convenciones de trabajo de Trackvance Core

El producto vigente es 0.8.0; la especificación técnica conserva su edición v1.1.
La fuente documental oficial está en el repositorio privado
https://github.com/eddiesan422/TrackvanceCore-docs. La carpeta local
`ProductOne/Documentación` es su checkout de trabajo, con sólo la edición vigente
visible. Las ediciones originales, publicaciones e informes anteriores se
conservan en etiquetas e historial Git del repositorio documental, con procedencia
y hashes comprobados. No inventar fechas, SHA, resultados ni origen de archivos.

## Aislamiento de desarrollo

- Trabajar en un checkout/worktree propio. No reconstruir, reiniciar, migrar ni
  cambiar la configuración de `trackvance-certification` durante desarrollo o CI.
- Proteger sus datos, PostgreSQL, claves, credenciales, imágenes, redes y volúmenes
  `postgres`/`trackvance_data`; proteger también `bikerwash` y recursos cuyo dueño
  o finalidad no estén comprobados.
- Cada prueba crea un proyecto Compose propio, puertos localhost y almacenamiento
  independientes, CPU/RAM/PIDs acotados y concurrencia limitada. Conservar primero
  los reportes útiles y limpiar únicamente recursos de esa identidad, incluso si
  falla. No ejecutar limpieza global ni `down -v` de la instalación habitual.
- Reutilizar imágenes sólo después de verificar SHA de código, etiqueta OCI e ID
  inmutable; conservar cachés recientes y dependencias bloqueadas.

## Perfiles de verificación y cierre

- `functional` es la regresión automática de GitHub Actions: lint, tipos, unitarias,
  contratos, migraciones y pruebas reales de todos los módulos con fixtures pequeños
  y deterministas. Mantener permisos, errores, integridad, asincronía, cancelación,
  reintentos, particiones y recuperación básica sin alterar límites de producto.
- `deep` es la certificación local por grupos: 400.000/1.000.000 registros,
  100/500/1024 MiB, variantes XLSX, Spark masivo, límites reales, concurrencia y
  recuperación prolongada. Conservar todas las pruebas intensivas históricas.
- Durante desarrollo ejecutar pruebas según impacto. No repetir toda la matriz
  masiva por cada ajuste ni por cambios exclusivamente documentales; éstos validan
  documentos y enlaces y dejan resuelto el check final estable.
- Distinguir explícitamente **CI funcional aprobado** de **volumetría/certificación
  profunda aprobada**. Un perfil no acredita el otro. La política reemplaza exigir
  toda la volumetría en Actions y repetir recorridos completos por documentación.
- El gate falla si faltan pruebas obligatorias o evidencia, o si terminaron fallidas
  o canceladas. Registrar SHA exacto, perfil, identidad de runner, entorno, tamaños,
  duración, recursos, resultados y pruebas no ejecutadas. Un runner local usa su
  identidad propia y nunca simula IDs de Actions. Si falta capacidad, registrar
  `PENDING_CAPACITY` (pendiente por capacidad) y continuar las pruebas que sí caben.
- Los informes conservan FAIL/CANCELLED/SKIP/pendientes históricos. Medir tiempo real
  y separar cambio de alcance de mejora de ejecución; no anticipar CI del SHA final
  ni resultados futuros. Un cambio sólo documental no invalida evidencia de producto
  cuyos bytes y alcance permanecen comprobados.

## Documentos, contratos e historia

- Mantener en código workflows, scripts, fixtures, contratos y generadores requeridos
  por build/tests. Actions debe funcionar sin clonar el repositorio privado de docs.
- Generar contratos desde el runtime con `scripts/export_contracts.py`. El PDF usa
  `scripts/docs/build_specification.py --repo <checkout-código> --docs-root
  <checkout-documental>/docs`; la fuente, parámetros y resultados viven en docs.
- Publicar PDF sólo tras renderizar e inspeccionar sus páginas; conservar hashes y
  el estado real de gates. La publicación del PDF no certifica código ni volumetría.
- Antes de retirar copias locales verificar mediante clone/fetch del remoto los
  bytes y las etiquetas que conservan cada original/publicación/evidencia útil.
  Deduplicar únicamente por hashes idénticos. Excluir secretos, datasets masivos,
  temporales y extracciones/cachés regenerables; no reescribir el historial de código.

Una actualización de la instalación habitual requiere autorización explícita
independiente, backup consistente verificado y restore en un proyecto propio.
