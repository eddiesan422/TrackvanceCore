# Especificación técnica

Esta carpeta versiona la **Especificación Técnica v1.1** y su fuente editable para
la evolución funcional local `0.4.0`. El nombre v1.1 identifica el documento;
0.4.0 identifica el software. IMPLEMENTADO, PREPARADO y OBJETIVO se distinguen
en el texto; la productización permanece fuera del roadmap local 1–9.

- `Trackvance_Core_Especificacion_Tecnica_v1.1.md`: fuente editable oficial.
- `Trackvance_Core_Especificacion_Tecnica_v1.1.pdf`: documento publicado tras
  generar candidato y revisar renderizado.
- `build_specification.py`: generador portable reportlab/pypdf, separado de las
  dependencias de ejecución del producto.
- `validation_results_0.4.0.json`: resultados del ciclo usados en la sección de
  aceptación; no se sustituyen con datos de la revisión anterior.
- `validation_results_0.3.0.json`: evidencia histórica de la entrega anterior.

## Generación y publicación

En un entorno de autoría con Python, reportlab y pypdf, desde el repositorio:

```sh
python docs/specification/build_specification.py
```

El generador usa esta fuente, `backend/openapi.json`, el logo del repositorio y
el JSON de resultados explícito. Reconoce el repositorio desde su ubicación;
`--repo`, `--source`, `--results` y `--output-dir` permiten rutas distintas.
Usa Arial en Windows y DejaVu/Vera cuando Arial no está disponible. No descarga
fuentes ni librerías durante la generación.

El candidato queda en `tmp/pdfs/specification-candidate.pdf`. Revisa las páginas
renderizadas, tablas, encabezados, índice y diagramas antes de publicar con:

```sh
python docs/specification/build_specification.py --publish
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

La certificación consolidada y el inventario de cambios están en
[validación](../development/validation.md). La publicación 0.4.0 tiene **31 páginas
y 25 marcadores**, todas renderizadas y revisadas, sin texto fuera de márgenes.
La copia oficial y la versionada son idénticas. SHA-256 del PDF publicado:
`62f04c3b3bc8a4e24bd3d22cc0f8f9c9212600d4b0215a82deaafa3e073bd64c`.
La sección de aceptación registra los seis jobs de CI aprobados y conserva la
incidencia Linux inicial junto con su corrección y recertificación.
