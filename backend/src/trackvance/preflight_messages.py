"""Functional preflight messages, independent of validation decisions.

Only caller-controlled schema identifiers may appear as context. Driver exception
text, row values and credentials are deliberately never accepted by this builder.
"""

from collections.abc import Mapping

# These pairs describe outcomes, never change the checks or their predicates.
MESSAGES: dict[str, tuple[str, str]] = {
    "DATASET_VERSION": ("La DatasetVersion inmutable está disponible.", "La DatasetVersion seleccionada no está disponible."),
    "CANONICAL_ARTIFACT": ("El artifact canónico está íntegro y es legible.", "No se pudo verificar la integridad del artifact canónico."),
    "DESTINATION": ("El destino y su revisión inmutable están disponibles.", "El destino o su revisión no están disponibles para la entrega."),
    "CONNECTION": ("La conexión al destino fue verificada.", "No se pudo verificar la conexión al destino."),
    "SOURCE_COLUMNS": ("Las columnas seleccionadas existen en la versión.", "Faltan columnas seleccionadas en la DatasetVersion."),
    "SOURCE_TYPE_PRESERVATION": ("El mapping conserva los tipos lógicos de la DatasetVersion.", "El mapping cambia tipos lógicos; Data Delivery no admite esa conversión."),
    "IDENTIFIER_PRESERVATION": ("El identificador conserva su representación textual.", "El identificador debe conservarse como texto para preservar su representación."),
    "COLUMN_MAPPING": ("El mapping técnico no tiene ambigüedades.", "El mapping contiene una incompatibilidad de tipos o identificadores; revise los checks de detalle."),
    "VALUES_COMPATIBLE": ("Todos los valores son compatibles con el mapping técnico.", "Hay valores incompatibles con el mapping técnico o columnas ausentes; revise los checks de detalle."),
    "SCHEMA_STATE": ("La existencia del schema coincide con la selección.", "La existencia del schema no coincide con la selección; revise si debía existir o ser nuevo."),
    "TARGET_ABSENT": ("La tabla nueva todavía no existe.", "La tabla seleccionada ya existe; CREATE_AND_LOAD requiere una tabla nueva."),
    "TARGET_EXISTS": ("La tabla destino existe.", "No se encontró la tabla destino en el schema seleccionado."),
    "PERMISSIONS": ("La cuenta del destino tiene los permisos requeridos para esta entrega.", "No se verificaron todos los permisos necesarios para escribir en la tabla seleccionada. Los permisos de Trackvance no conceden privilegios SQL remotos."),
    "TARGET_COLUMN_EXISTS": ("La columna destino existe.", "No existe la columna destino seleccionada."),
    "TARGET_COLUMN_WRITABLE": ("La columna admite escritura explícita.", "La columna es generada o identity y no admite este mapping de escritura explícita."),
    "TYPE_COMPATIBLE": ("El tipo configurado es compatible con el target.", "El tipo del target no ofrece almacenamiento exacto compatible con el tipo configurado."),
    "LENGTH_COMPATIBLE": ("La longitud configurada y observada cabe en el target, medida en UTF-16.", "La longitud configurada u observada excede la capacidad UTF-16 del target."),
    "STRING_STORAGE_COMPATIBLE": ("La familia Unicode y la collation preservan el texto.", "El tipo o la collation del target alteraría el texto Unicode."),
    "DECIMAL_COMPATIBLE": ("La precisión, escala y capacidad de dígitos enteros son suficientes.", "El target no tiene precisión, escala o capacidad de dígitos enteros suficiente para almacenar el decimal exacto."),
    "TIMESTAMP_COMPATIBLE": ("La precisión temporal y el instante se conservan.", "El target alteraría la precisión temporal o el instante con offset."),
    "INTEGER_RANGE_COMPATIBLE": ("Los valores están dentro del rango entero del target.", "Hay valores fuera del rango entero admitido por el tipo del target."),
    "NULLABILITY_COMPATIBLE": ("La nulabilidad del mapping y los valores es compatible con el target.", "El mapping o los valores admiten null donde el target exige un valor no nulo."),
    "REQUIRED_TARGET_COLUMNS": ("Todas las columnas obligatorias del target están cubiertas.", "Faltan columnas obligatorias del target sin default, generación o identity."),
    "UPSERT_UNIQUE_CONSTRAINT": ("La clave UPSERT está respaldada por una PK o restricción unique compatible.", "No se verificó una PK o restricción unique compatible que respalde la clave UPSERT."),
    "UPSERT_SOURCE_KEYS": ("Las claves UPSERT de toda la fuente no tienen nulls ni duplicados.", "Las claves UPSERT de la fuente contienen nulls o duplicados."),
    "SQLSERVER_IGNORE_DUP_KEY": ("Los índices UNIQUE propagan duplicados como errores.", "El target usa IGNORE_DUP_KEY y podría omitir filas silenciosamente."),
    "OVERWRITE_ROW_SECURITY": ("DELETE puede ver el conjunto completo del target.", "Una política de seguridad por filas podría ocultar filas a DELETE; OVERWRITE no es seguro."),
    "AUDIT_ALTER_PERMISSION": ("La cuenta SQL tiene permiso ALTER para agregar los campos de auditoría requeridos.", "No se verificó permiso ALTER remoto para agregar los campos de auditoría requeridos."),
    "LOCAL_PAYLOAD": ("Los identificadores y el payload tipado están preparados sin iniciar escritura remota.", "No se pudo preparar el payload tipado localmente; no se inició escritura remota."),
    "LOCAL_RESOURCES": ("El volumen y la estructura cumplen los límites efectivos de esta instalación.", "El volumen o la estructura excede un límite efectivo o no coincide con la metadata de la versión."),
    "INVALID_IDENTIFIER": ("El identificador SQL es válido.", "Un identificador SQL no cumple el contrato del motor destino."),
    "INVALID_TYPE_PARAMETERS": ("Los parámetros del tipo son válidos.", "La longitud, precisión o escala configurada no cumple el contrato del tipo."),
    "UNSUPPORTED_TARGET_TYPE": ("El tipo técnico está soportado.", "El tipo técnico seleccionado no está soportado."),
    "VALUE_NOT_COMPATIBLE": ("Los valores cumplen el tipo técnico.", "Un valor no cumple el tipo, rango, precisión, longitud o nulabilidad configurados."),
    "VALUE_TYPE_MISMATCH": ("Los valores cumplen el tipo técnico.", "Un valor no cumple el tipo o rango configurado, o contiene caracteres no almacenables."),
    "NULLABILITY_MISMATCH": ("Los valores cumplen la nulabilidad configurada.", "Hay nulls en una columna declarada no nula en el mapping."),
    "STRING_LENGTH_EXCEEDED": ("El texto cabe en la longitud configurada.", "Hay texto cuya longitud UTF-16 excede el límite configurado en el mapping."),
    "DECIMAL_PRECISION_EXCEEDED": ("Los decimales cumplen la precisión y escala configuradas.", "Hay decimales cuya precisión o escala excede la capacidad configurada en el mapping."),
    "AUDIT_MAPPING_COLLISION": ("El mapping respeta las columnas de auditoría.", "El mapping intenta escribir una columna reservada de auditoría."),
    "AUDIT_COLUMNS_DRIFT": ("Se conservan las columnas de auditoría requeridas.", "La tabla perdió columnas de auditoría requeridas por su política permanente."),
    "AUDIT_COLUMNS_INCOMPATIBLE": ("Las columnas de auditoría son compatibles.", "Los nombres o tipos existentes de auditoría no son compatibles."),
    "UPSERT_CONSTRAINT_MISSING": ("La restricción UPSERT exacta está disponible.", "La restricción UPSERT exacta no está disponible."),
}


def preflight_message(code: str, passed: bool, context: Mapping[str, str] | None = None) -> str:
    pair = MESSAGES.get(code)
    message = pair[0 if passed else 1] if pair else (
        "La comprobación se completó satisfactoriamente." if passed
        else "La comprobación no se completó satisfactoriamente; consulte su código de diagnóstico."
    )
    column = (context or {}).get("column", "")
    if column and len(column) <= 128 and not any(ord(c) < 32 for c in column):
        return f"Columna {column}: {message}"
    return message
