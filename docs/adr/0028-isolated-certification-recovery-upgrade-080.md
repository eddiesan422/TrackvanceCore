# ADR 0028: certificación aislada, recuperación y promoción de 0.8.0

Fecha: 2026-10-05. Estado: aceptada para la implementación; resultados en validation.md.

La instalación habitual conserva datos, volúmenes, secretos, origen web y horarios.
Certificar Catálogo/Reportes exige destruir y restaurar fixtures y medir poblaciones
completas. Cada ciclo utiliza un UUID Compose propio, PostgreSQL sintético, volúmenes
y red exclusivos, puertos loopback 32000–32999 y una configuración privada distinta.
`certification_v080.py` inventaría la instalación real por labels y valida la
configuración Compose resuelta antes de crear o eliminar recursos. Rechaza recursos
externos/compartidos, puertos protegidos, mounts solapados, socket Docker, privilegios,
SSO real, autoarranque y servicios sin límites CPU/RAM. El presupuesto conjunto es
9 GiB; los procesos SQL tienen sus propias cotas. Se compara el inventario habitual
antes y después, incluyendo imágenes, mounts, estado, puertos y restart policy.

0017 es aditiva sobre 0016. El inventario nativo contiene 55 tablas; state.json es
schema 8 y backup-manifest.json mantiene schema 2. La proyección de state 7 admite
únicamente trece tablas nuevas vacías, campos Dataset NULL/1/UNKNOWN y la referencia
Job REPORT NULL. No atribuye clasificación histórica ni índices de aprobación a
ejecuciones previas. Toda columna, FK, tabla o cambio histórico desconocido rechaza
la comparación. Las proyecciones anteriores encadenan las verificaciones existentes.
La recuperación nativa compara también contextos congelados, dependencias y linaje.
El archivo del volumen de datos excluye sólo la raíz privada `report-staging`;
los artefactos publicados, descriptores y partes canónicas siguen verificándose.

La promoción requiere suites locales, navegador real, poblaciones 400k/1M por fuente,
recuperación nativa e histórica y todos los jobs del SHA final en GitHub Actions.
Los fallos se conservan y se corrigen; no se eliminan suites ni se convierten skips
en PASS. El PDF completo se genera desde fuente editable y se revisa visualmente.
El SHA final y el upgrade se registran fuera del PDF para evitar una referencia circular.

Después de los gates se conserva el Compose realmente usado, incluidos overlays y
.env privados. Se obtiene un respaldo fresco verificado y se restaura en aislamiento.
Se construyen imágenes para todos los servicios afectados, se aplica sólo 0017 y se
comprueba lectura/metadata/integridad sin ejecutar conexiones o automatizaciones reales.
Los horarios/pausas, secretos, políticas de reinicio, volumen y puerto permanecen
según el inventario protegido. Un bloqueo real mantiene abierta la promoción; no
habilita reset, down -v sobre el principal, force push ni limpieza global de Docker.
