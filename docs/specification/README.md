# Especificación técnica

Esta carpeta versiona la **Especificación Técnica v1.1** y su fuente editable para
el endurecimiento local `0.5.1`. El nombre v1.1 identifica el documento;
0.5.1 identifica el software. IMPLEMENTADO, PREPARADO y OBJETIVO se distinguen
en el texto; la productización permanece fuera del roadmap local 1–10.

- `Trackvance_Core_Especificacion_Tecnica_v1.1.md`: fuente editable oficial.
- `Trackvance_Core_Especificacion_Tecnica_v1.1.pdf`: documento publicado tras
  generar candidato y revisar renderizado.
- `build_specification.py`: generador portable reportlab/pypdf, separado de las
  dependencias de ejecución del producto.
- `validation_results_0.5.1.json`: resultados ejecutados y límites explícitos
  de esta revisión; no reutiliza cifras anteriores como certificación.
- `validation_results_0.5.0.json`: resultados históricos 0.5.0, preservados.
- `validation_results_0.4.1.json`: evidencia histórica de la entrega anterior.
- `validation_results_0.4.0.json`: evidencia histórica de la entrega anterior.
- `validation_results_0.3.0.json`: evidencia histórica de la entrega anterior.

## Generación y publicación

La ampliación 0.5.1 desarrolla Delivery en 26 subsecciones A-Z, ocho ajustes con
antes/después/archivos/contratos/compatibilidad y casos de los otros módulos.
El generador admite índice y marcadores de dos niveles, cuatro diagramas Delivery
y bloques JSON paginables. La salida es determinista con los mismos inputs;
permite contrastar el hash del candidato revisado con el PDF publicado.
Mientras los gates estén en curso se usa `--draft` y no se publica.

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
[validación](../development/validation.md) y el [informe A-M](../development/release-report-0.5.1.md).

## Publicación 0.5.1

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
