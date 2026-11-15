# Contabilidad a Medida

Aplicación de escritorio para Windows que organiza carteras de residentes y no residentes, documentos PDF, cálculos y expedientes Excel. Desarrollada con Python y Flet; la lectura y el asistente con Claude son opcionales y requieren la clave del usuario en su equipo.

**[Descargar el instalador para Windows](https://github.com/deibyhenao/contabilidad-a-medida/releases/latest)**

El instalador permite elegir la carpeta de datos y no requiere tener Python instalado. La aplicación avisa de nuevas versiones, permite descargarlas en segundo plano y ofrece reiniciar e instalar cuando termine el trabajo en curso.

Los datos de clientes, las bases de datos y las claves **no se incluyen** en este repositorio ni en las releases. Las actualizaciones conservan los datos del usuario.

Consulta [la guía de instalación y publicación](GUIA_ACTUALIZACIONES.md) para compilar, publicar versiones y conocer los límites de la verificación y de la firma del ejecutable.

## Desarrollo

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe main.py
```

Las dos plantillas de `plantillas/` se conservan como recursos originales. La aplicación genera una copia para cada expediente; no se deben modificar sus fórmulas, hojas ni columnas. Antes de actualizar un Excel de cliente se importan sus cambios manuales.

En los libros generados, las imputaciones pendientes redondean base y cuota a dos decimales. Las presentadas conservan los importes de su justificante con comentario. Estas excepciones están acotadas a las columnas H/J de la hoja de imputaciones; las plantillas originales no cambian.

## Otros activos y normativa de Patrimonio

En No residentes, «Otros activos» permite registrar cuentas, criptoactivos y valores,
revisar su ubicación y valoración y actualizar el 714/718 y el Excel del expediente.
Los resúmenes de cartera son informativos: no se suman otra vez a sus posiciones.
El importe total de una posición es distinto del precio por unidad.

La normativa se elige una vez por cliente y ejercicio. Los borradores posteriores
sin elección propia heredan la última anterior; se puede cambiar en un año sin
alterar los anteriores ni las declaraciones presentadas. Para un no residente,
la opción autonómica depende del mayor valor de sus bienes y derechos sujetos en
España, no del domicilio del asesor. Esta elección afecta al Patrimonio, no al 210.

La matriz general contrastada cubre las quince comunidades de régimen común para
2025, y Cataluña, Comunitat Valenciana y Madrid también para 2024/2026. Los ejercicios
abiertos y los beneficios particulares, convenios y límites conjuntos que necesitan
otros datos se señalan como provisionales. Los regímenes forales quedan fuera del
alcance AEAT estatal de esta ampliación. No se aplican reglas desconocidas como si
fueran una liquidación definitiva.

Los libros antiguos mantienen su nombre y sus hojas originales. Al registrar activos
se añade `16_OTROS_ACTIVOS` a la copia del cliente, sin modificar las plantillas originales.
Si una exportación queda pendiente, la sincronización conserva cambios hechos en
campos distintos desde Excel y desde la app. Si ambas fuentes modifican un mismo
campo de forma diferente, se detiene para conciliarlos; no decide cuál sobrescribir.
Las hojas personales adicionales se detectan antes de regenerar el libro.

Esta ampliación se incorpora a la versión 1.0.8.
