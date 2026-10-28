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
