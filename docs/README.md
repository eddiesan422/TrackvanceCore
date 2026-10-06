# Documentación y contratos de Trackvance Core

La fuente documental oficial es el repositorio privado
[TrackvanceCore-docs](https://github.com/eddiesan422/TrackvanceCore-docs).
Su checkout local vigente es ProductOne/Documentación. Guías, decisiones,
especificación v1.1/PDF e históricos se mantienen allí; producto 0.8.0 y versión
documental v1.1 son identidades diferentes. Los originales retirados de este
árbol se verificaron por SHA-256 en etiquetas remotas antes de eliminarlos.

En el código se conservan los contratos generados usados por runtime/CI:

- [Modelos 0.8.0](specification/model_contract_0.8.0.json).
- [Permisos 0.8.0](specification/permission_contract_0.8.0.json).
- [Matriz de permisos generada](development/permission-matrix.md).

`scripts/export_contracts.py` genera estos archivos y Actions verifica su diff
sin descargar documentación privada. El generador de la especificación queda
en `scripts/docs/build_specification.py`; recibe `--docs-root` del checkout
documental. Las instrucciones de publicación y revisión visual están en
[la guía oficial](https://github.com/eddiesan422/TrackvanceCore-docs/blob/main/docs/specification/README.md).

[AGENTS.md](../AGENTS.md) define la política functional/deep, sus evidencias
independientes y la protección de instalaciones activas.
