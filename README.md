# Lluvia en Santiago (WRF-SMN)

App web instalable (PWA) que muestra la precipitación pronosticada para Santiago en las
próximas 72 horas, con gráfico hora a hora y mapas animados, usando el pronóstico WRF 4 km
del SMN Argentina publicado como datos abiertos en `s3://smn-ar-wrf`.

GitHub hace todo el trabajo: cada hora revisa si hay una corrida nueva del modelo, la
procesa en sus máquinas y publica la app en GitHub Pages, en `lluvia.lab34.cl`.

## Estructura

    .github/workflows/pronostico.yml   la automatización (GitHub Actions)
    backend/wrf_santiago.py            descarga y procesa el pronóstico
    backend/requirements.txt           librerías de Python
    web/                               la app (index.html, sw.js, manifest, íconos)

---

## Puesta en marcha

### 1. Crear el repositorio

1. Entra a github.com (crea cuenta si no tienes) y pulsa **New repository**.
2. Nombre: `lluvia-santiago`. Déjalo **Public**: GitHub Pages y Actions son gratis
   para repositorios públicos.
3. No marques "Add a README"; el proyecto ya trae uno. Pulsa **Create repository**.

### 2. Subir los archivos

Con git desde tu computador (recomendado, así aprendes el flujo de trabajo normal):

    cd lluvia-santiago
    git init
    git add .
    git commit -m "Primera versión"
    git branch -M main
    git remote add origin https://github.com/TU_USUARIO/lluvia-santiago.git
    git push -u origin main

Al hacer `push` te pedirá autenticarte. GitHub ya no acepta la contraseña en la terminal:
crea un token en Settings > Developer settings > Personal access tokens, o instala
GitHub CLI (`gh auth login`), que lo resuelve solo.

Ojo: la carpeta `.github` empieza con punto y queda oculta en Windows y Mac. Si subes los
archivos arrastrándolos en la web, revisa que esa carpeta haya subido, porque sin ella no
hay automatización.

### 3. Activar GitHub Pages

En el repositorio: **Settings > Pages > Build and deployment > Source: GitHub Actions**.

### 4. Primera prueba con datos de demo

1. Pestaña **Actions** > "Pronóstico WRF-SMN" > **Run workflow**.
2. Marca **Usar datos de prueba** y ejecuta.
3. Entra a la ejecución para ver cada paso en vivo. Al terminar, el paso de deploy muestra
   la URL: `https://TU_USUARIO.github.io/lluvia-santiago/`.

### 5. Prueba con datos reales

Ejecuta de nuevo **Run workflow**, ahora sin marcar nada. Revisa el registro del paso
"Procesar pronóstico". Como el script no se pudo probar contra el bucket real, puede que
haya que ajustar alguno de estos puntos (están al inicio de `wrf_santiago.py`):

- **Nombre de los archivos** (`FILE_RE`, hoy `WRFDETAR_01H_AAAAMMDD_HH_FFF.nc`). Puedes
  revisarlo con `aws s3 ls --no-sign-request s3://smn-ar-wrf/DATA/WRF/DET/`.
- **Nombre de la variable de precipitación** (`PRECIP_VARS`). Si no la encuentra, el error
  lista todas las variables del archivo.
- **Si la precipitación es por hora o acumulada**: si los totales salen absurdamente altos,
  agrega `--pp-mode cumulative` en la línea del script en `pronostico.yml`.

Cada corrección es editar, `git commit`, `git push` y volver a ejecutar.

### 6. Tu dominio: lluvia.lab34.cl

1. En el **cPanel de Planeta Hosting > Zone Editor** del dominio `lab34.cl`, agrega un registro:
   - Tipo: `CNAME`
   - Nombre: `lluvia`
   - Destino: `TU_USUARIO.github.io`
2. En GitHub: **Settings > Pages > Custom domain**: `lluvia.lab34.cl` y guarda. GitHub
   verifica el DNS (puede tardar desde minutos hasta unas horas).
3. Cuando aparezca disponible, marca **Enforce HTTPS**.
4. En **Settings > Secrets and variables > Actions > Variables**, crea la variable
   `SITE_URL` con el valor `https://lluvia.lab34.cl`. Con eso el flujo sabe qué corrida ya
   está publicada y no reprocesa ni republica si no hay nada nuevo.

Listo: abre `lluvia.lab34.cl` en Chrome de Android y elige **Instalar app**.

---

## Bueno saber

- **Las tareas programadas se pausan** si el repositorio pasa 60 días sin actividad. GitHub
  avisa por correo; basta con reactivarlas en la pestaña Actions o hacer cualquier commit.
- **El horario no es exacto**: GitHub puede atrasar las tareas programadas unos minutos
  cuando hay mucha demanda.
- **Probar en tu computador**: `pip install -r backend/requirements.txt`, copia `web/` a
  una carpeta `site/`, corre `python backend/wrf_santiago.py --out site --demo` y luego
  `python -m http.server -d site 8000`.

## Personalizar

- Puntos: lista `POINTS` en el script (con más de uno, la app muestra un selector).
- Zona del mapa: `BBOX`. Escalas de color: `PALETTES` (la app las lee del JSON).
