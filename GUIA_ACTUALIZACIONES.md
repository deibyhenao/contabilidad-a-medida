# Instalador y actualizaciones de Contabilidad a Medida

## Para quien instala el programa

1. Descarga `ContabilidadAMedida-<versión>-windows-x64-Setup.exe` de **Releases** del repositorio oficial.
2. El instalador está en español, no requiere Python ni permisos de administrador. Permite escoger dónde guardar los datos. La carpeta del programa y la de los datos deben estar separadas.
3. Abre la aplicación desde el menú Inicio. Al iniciar comprueba si existe una versión estable nueva. Si no hay conexión puedes seguir trabajando y buscar actualizaciones después.
4. El aviso **Actualizar ahora** descarga el instalador en segundo plano. Puedes cancelar la descarga o seguir trabajando.
5. Cuando aparece **Reiniciar e instalar**, guarda tu trabajo y púlsalo. Si hay una importación, IA o exportación en marcha, espera a que termine. La aplicación se cierra, el instalador sustituye sus archivos y vuelve a abrirla.

Las actualizaciones y la desinstalación conservan los datos. El programa no ejecuta SQL ni mueve expedientes desde el actualizador. Las migraciones de esquema del programa se siguen realizando al abrir la base de datos mediante el motor existente.

## Dónde se guarda cada cosa

| Contenido | Ubicación |
|---|---|
| Programa y bibliotecas | `%LOCALAPPDATA%\Programs\ContabilidadAMedida` por defecto |
| Puntero a la carpeta elegida | `%LOCALAPPDATA%\ContabilidadAMedida\ubicacion.ini` |
| Base SQLite, copias y carteras iniciales | Carpeta elegida en el instalador |
| Descargas y registro de instalación OTA | `%LOCALAPPDATA%\ContabilidadAMedida\Actualizaciones` |

En desarrollo se mantienen las rutas previas del proyecto. Instalar el programa **no importa automáticamente** los expedientes de la copia de desarrollo. Para trasladarlos: cierra la app, copia de forma coherente la base de datos y sus carpetas a la ubicación elegida, conserva un respaldo y comprueba las rutas de los clientes. No sustituyas una base abierta ni mezcles una base antigua con carpetas nuevas. Si ya tienes datos instalados, una reinstalación conserva el puntero existente.

El botón actual «Carpeta de clientes» conserva su comportamiento: cambia la ubicación para expedientes, no traslada la base SQLite ni los expedientes existentes. El almacén inicial de la instalación se configura en el instalador.

## Publicar la siguiente versión

La versión se cambia en **un solo archivo**, `version.py`, por ejemplo de `1.0.0` a `1.0.1`.

Trabaja desde un clon del repositorio público. **No empujes el historial del proyecto original:** contiene una base de datos histórica. Para exportar los cambios desde esa copia antigua a un clon público limpio:

```powershell
.\.venv\Scripts\python.exe scripts/preparar_publicacion.py --destino "C:\ruta\contabilidad-publicacion"
```

Revisa y confirma los archivos en ese clon; allí ejecuta los comandos de publicación:

```powershell
git add -- <archivos de código revisados>
git commit -m "Publicar versión 1.0.1"
git push origin main
git tag v1.0.1
git push origin v1.0.1
```

El workflow `.github/workflows/windows.yml` ejecuta la suite completa en Windows. Solo si pasa, compila, diagnostica el ejecutable con datos ficticios, genera el instalador y publica la release con `SHA256SUMS.txt`. El tag debe coincidir exactamente con `version.py`; no se reutilizan tags ni se sobrescriben releases publicadas. No hacen falta claves API de clientes para las pruebas ni para compilar.

También puedes editar el código en GitHub y crear una nueva release con un tag `v<versión>` apuntando a `main`: el workflow construye y publica sus assets. Para dejar que el workflow cree la release por sí solo, crea el tag desde un clon. Quien desarrolla necesita acceso de escritura a GitHub; los usuarios finales no necesitan cuenta ni token.

No subas bases SQLite, expedientes, PDFs, archivos `.env`, claves de firma ni copias. El paquete se construye a partir de módulos importados, `assets`, las dos plantillas originales y el auxiliar de actualización; no se empaqueta toda la carpeta del proyecto.

## Compilar localmente

Usa Windows x64 y Python 3.10. Instala Inno Setup 6 desde su web oficial. Se recomienda un entorno separado del entorno de desarrollo:

```powershell
py -3.10 -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
.\.venv-build\Scripts\python.exe scripts/build_release.py --iscc "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
```

El resultado queda en `release/`. `scripts/build_release.py` falla si encuentra datos de clientes en el paquete o si el diagnóstico del ejecutable falla. La compilación necesita conexión para obtener el cliente oficial Flet; el cliente final lo recibe incluido y no tiene que descargarlo por separado.

## Seguridad y recuperación

- Solo se aceptan releases estables del repositorio configurado y versiones numéricas superiores. Se rechazan previews, versiones ambiguas, assets duplicados, tamaños inválidos y URLs ajenas.
- La descarga usa HTTPS, límites de tamaño, cancelación y SHA-256 de GitHub obligatorio. El auxiliar vuelve a verificar el hash antes de ejecutar el instalador.
- Si falta conexión, permisos, espacio o integridad, no se instala. Si falla la preparación, la app continúa abierta. El auxiliar espera a que termine el proceso; no lo mata ni sustituye archivos mientras está en uso.
- Inno Setup conserva los archivos anteriores durante su instalación y tiene su mecanismo de reversión ante errores de instalación. Esto no revierte migraciones fiscales o de base de datos posteriores al primer arranque.
- La huella protege la integridad respecto a GitHub; no sustituye una firma del editor. Esta primera entrega no incorpora un certificado Authenticode comercial. Windows puede mostrar un aviso de editor desconocido/SmartScreen. Para distribución comercial con identidad verificada, incorpora tu certificado en el paso de compilación y protege sus secretos fuera del repositorio.
- El código no incorpora tokens de GitHub en la app. Las descargas de usuarios requieren un canal público de releases.

Fuentes oficiales: [GitHub Releases API](https://docs.github.com/en/rest/releases/releases), [Flet y PyInstaller](https://flet.dev/docs/publish/using-pyinstaller/), [Inno Setup](https://jrsoftware.org/ishelp/), [recursos en PyInstaller](https://pyinstaller.org/en/stable/runtime-information.html).
