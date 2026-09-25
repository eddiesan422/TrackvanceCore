# Carga diferida del frontend: medición 0.5.1

Fecha: 25 de septiembre de 2026. Alcance: build de producción y pruebas del
frontend local. La certificación integrada con API, Docker, PostgreSQL y SQL
Server se registra por separado en [validación](validation.md).

## Resultado y límites

El entry JavaScript pasó de **611.407 a 364.966 bytes**: reducción de 246.441
bytes (**40,31 %**). Comprimido con gzip pasó de **179.471 a 116.768 bytes**:
reducción de 62.703 bytes (**34,94 %**). El build final no produjo el warning
de chunk mayor de 500 kB que sí produjo la baseline. No se elevó ese umbral.

Son tamaños reales de artifacts minificados, no una estimación de velocidad.
El entry no es la suma de todos los módulos que necesita una ruta autenticada:
la ruta elegida y sus dependencias compartidas se descargan al usarlas. Visitar
todas las secciones puede descargar más chunks; se difiere código que antes se
entregaba siempre, no se afirma haber eliminado todas esas funciones. No se
midieron Core Web Vitals, tiempo de interacción, latencia de red ni throughput
backend, y estos números no se presentan como mejoras de esas variables.

## Condiciones de la comparación

| Condición | Antes | Después |
| --- | --- | --- |
| Fuente | commit `e7838c3c86b2117605a889f23242e75d9b09577d` | working tree candidato 0.5.1, incluida la UI nueva de reparación/revisión |
| Comando | `pnpm build` en `frontend/` | mismo comando |
| Node / Vite | 22.14.0 / 7.3.6 | 22.14.0 / 7.3.6 |
| Módulos transformados | 1.714 | 1.718 |
| Entry JS | `index-CCebnB5P.js` | `index-Dcon_AYs.js` |
| Entry JS bytes / gzip bytes | 611.407 / 179.471 | 364.966 / 116.768 |
| CSS principal bytes / gzip bytes | 88.933 / 18.495 | 72.918 / 15.492 |
| Warning de chunk > 500 kB | Sí | No |

La comparación no es un experimento de splitting aislado: el candidato también
incluye las acciones y textos funcionales solicitados para 0.5.1. Se ejecutó el
build previo antes de cambiar el frontend, en el mismo entorno. Los tamaños
gzip proceden de `node:zlib.gzipSync` sobre cada archivo; kB utiliza 1.000 bytes.
Los nombres incluyen hashes del contenido y cambiarán con futuros builds.

La primera medición posterior al splitting produjo un entry de 364.966 bytes
(116.764 gzip) y un detalle Delivery de 17.395 bytes (5.549 gzip), con build de
1,96 s. La medición final de este documento incluye además el ajuste de
publicación tardía de evidencia descrito abajo: entry de 364.966 bytes
(116.768 gzip), detalle de 18.331 bytes (5.820 gzip) y build de 2,15 s. Los campos
`initial_after` e `initial_reduction` del JSON conservan íntegra la primera
medición, no se presentan como el último build.

## Decisión de implementación

`App.tsx` conserva eager el shell, sesión, navegación, header, footer y elementos
compartidos de autenticación. Declara imports dinámicos nativos de React para
Dashboard, historial, Datasets, Conexiones, Intake/ReconOps/Sentinel, Delivery y
las pantallas de Operaciones. Las exportaciones de una misma unidad funcional
comparten su módulo; no se introdujo un chunk por cada botón o formulario.

`RouteContent.tsx` mantiene navegación disponible mientras Suspense presenta
**Cargando sección…**. Un error boundary muestra un fallo explícito con **Recargar
página** si no se puede obtener o renderizar la sección. Su key por pathname
permite abandonar la ruta fallida y abrir otra. No se aplica recarga automática
infinita ni se oculta un fallo como contenido vacío.

`RunDetail` importa `DeliveryRunDetail` sólo para un Run Delivery. Separarlo de
`Delivery.tsx` evita cargar el builder/destinos por abrir ese detalle. Se mantienen
los guards existentes de permisos y la API sigue siendo la autoridad final;
React.lazy no es una frontera de seguridad. El CSS de cada unidad se conserva
con su import. No se agregaron dependencias ni `manualChunks` arbitrarios.

Dentro del flujo de reparación de evidencia, `DeliveryRunDetail` contempla que
el backend persista `SUCCESS` / `COMMITTED` antes de publicar receipt/manifest.
Mientras falta el receipt, consulta sólo el Run cada 1,5 s por un máximo de
60 s, reutiliza una consulta lenta en curso y bloquea las descargas. La llegada
del receipt o de `PENDING_REPAIR` termina esa espera. Al vencer el plazo ofrece
**Actualizar estado**, otra lectura explícita; no reenvía datos ni inventa un
estado de reparación. Las respuestas y rerenders no reinician el plazo, y los
permisos existentes siguen aplicándose a descargas y acciones.

### Chunks funcionales posteriores

| Chunk JS | Bytes | Gzip bytes |
| --- | ---: | ---: |
| Entrada `index` | 364.966 | 116.768 |
| `Runs` | 81.005 | 22.689 |
| `Delivery` | 49.127 | 13.630 |
| `Operations` | 35.973 | 9.852 |
| `Datasets` | 25.330 | 8.639 |
| `Dashboard` | 20.761 | 6.464 |
| `Connections` | 19.795 | 5.828 |
| `DeliveryRunDetail` | 18.331 | 5.820 |
| `VersionIdentity` | 4.893 | 1.951 |
| `ColumnMultiSelect` | 3.620 | 1.635 |
| `History` | 1.827 | 962 |

Vite emitió **19 archivos JS y cuatro CSS**, incluidos chunks compartidos
pequeños de iconos. El inventario completo con nombre, bytes, gzip y metadatos
está en [frontend-bundle.json](evidence/0.5.1/frontend-bundle.json). No deben
sumarse indiscriminadamente todos esos tamaños para describir la primera ruta.

## Verificación ejecutada

| Comprobación | Resultado medido |
| --- | --- |
| `pnpm lint` | PASS |
| `pnpm typecheck` | PASS |
| `pnpm test` | 154 pruebas / 17 archivos, PASS, 10,96 s |
| `pnpm build` | PASS, 2,15 s en la ejecución final registrada |
| Playwright `delivery-operations.spec.ts` | 7 PASS, 0 fallos/skips/retries, 13,6 s |

Las pruebas unitarias nuevas cubren loader con shell visible, fallo de import y
recuperación al navegar, acceso directo a rutas diferidas y guards de builders,
acciones de reparación/revisión, roles de lectura, fallos cerrados y métricas
null frente a cero. Seis regresiones de publicación tardía verifican receipt,
`PENDING_REPAIR`, límite temporal/actualización manual sin POST, lectura sin
permisos de escritura, reutilización de GET lento y limpieza al desmontar. Las
pruebas existentes siguen incluidas en las 154.

Los siete escenarios de navegador usaron Chrome contra `vite preview` del build
de producción en `127.0.0.1:33157`, **con toda la API interceptada**. Cubren
navegación y chunks, deep links, recarga, revisión UNKNOWN persistida en el
fixture, reparación de evidencia y ausencia de controles para lectura. Incluyen
receipt y `PENDING_REPAIR` tardíos sin recargar, con descargas bloqueadas durante
la espera. Verifican que esas acciones no solicitan crear otra Run. No son pruebas de SQL, de
persistencia de una base real ni de autorización backend: esos resultados deben
venir de los runners integrados. El preview aislado se detuvo al terminar.

Un primer ensayo de navegador falló por esperar dos nodos de texto separados
para `N/D / N/D`; se corrigió el selector al valor completo de la métrica y se
repitieron los cinco escenarios con éxito, sin cambio del código del producto.
También se corrigió una opción no soportada de Testing Library detectada por
TypeScript en las pruebas nuevas. La evidencia no oculta esas iteraciones:
[frontend-tests.json](evidence/0.5.1/frontend-tests.json).

La primera ejecución completa obtuvo 148 pruebas unitarias / 16 archivos en
10,77 s y cinco escenarios de navegador en 8,7 s. El JSON conserva esos
resultados en `initial_vitest` e `initial_playwright_deterministic`; los campos
principales y la tabla anterior corresponden a la ejecución final de 154 y siete.

El E2E real opt-in de Delivery conserva `TV_DELIVERY_E2E`; añade la comprobación
de etiquetas/ayuda y recarga de la URL de un Run confirmado. Su resultado se
publica con el ciclo aislado de Delivery, no se infiere de los siete mocks.

## Reproducción y despliegue

Desde `frontend/`, con las dependencias del lockfile:

```powershell
pnpm lint
pnpm typecheck
pnpm test
pnpm build
pnpm exec vite preview --host 127.0.0.1 --port 33157 --strictPort
```

En otra terminal, desde el mismo directorio:

```powershell
$env:PLAYWRIGHT_BASE_URL = 'http://127.0.0.1:33157'
pnpm test:e2e tests-e2e/delivery-operations.spec.ts
```

Detener el preview al terminar. No apuntar fixtures o runners destructivos a la
instalación habitual. Los pasos de Docker, backup previo, reconstrucción de
imágenes y conservación de volúmenes están en [operación](operations.md).
Arrancar un contenedor antiguo desde Docker Desktop no incorpora un build nuevo.

El HTML y los assets de un build deben desplegarse juntos. Una pestaña antigua
puede solicitar un hash de chunk que ya no existe tras reemplazar el despliegue;
el boundary ofrece recarga explícita para obtener el HTML vigente. La navegación
directa depende asimismo del fallback SPA de nginx ya existente y se comprueba
en los E2E integrados; no basta con que funcione el servidor de desarrollo.
