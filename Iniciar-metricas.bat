@echo off
rem Levanta la pila de metricas de OpenCode (exportador, Prometheus y Grafana) y abre Grafana.
cd /d "%~dp0"

if not exist ".env" (
  echo Falta el fichero .env. Copia .env.example a .env y define GRAFANA_ADMIN_PASSWORD.
  pause
  exit /b 1
)

docker info >nul 2>&1 && goto up

echo Docker no esta en marcha. Iniciando Docker Desktop...
start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
set /a n=0

:wait
timeout /t 5 /nobreak >nul
docker info >nul 2>&1 && goto up
set /a n+=1
if %n% GEQ 36 goto fail
echo Esperando a Docker... (%n%/36)
goto wait

:up
echo Levantando la pila de metricas de OpenCode...
docker compose up -d
if errorlevel 1 goto fail
start "" http://localhost:3001
echo.
echo Listo. Grafana: http://localhost:3001
timeout /t 5 >nul
exit /b 0

:fail
echo.
echo ERROR: no se pudo levantar la pila. Revisa que Docker Desktop este instalado y arrancado.
pause
exit /b 1
