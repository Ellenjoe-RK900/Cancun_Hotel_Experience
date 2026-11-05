# Cancún Hotel Experience

Proyecto educativo de catálogo y reservaciones de hoteles ficticios inspirados en Cancún, desarrollado con Flask, MongoDB Atlas y Bootstrap.

## Funciones
- Registro e inicio de sesión con contraseñas cifradas (hash).
- Administrador con CRUD de hoteles.
- Búsqueda por nombre y zona.
- Reservación simulada con fechas, huéspedes y total estimado.
- Inventario global decrementado de forma atómica al reservar.
- Cancelación e historial de reservaciones.
- API pública: `GET /api/hoteles` y `GET /api/hoteles/<id>`.

## Instalación
1. `python -m venv .venv`
2. Windows PowerShell: `.\.venv\Scripts\Activate.ps1` (si bloquea scripts: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`).
3. `pip install -r requirements.txt`
4. Copia `.env.example` a `.env` y configura `MONGO_URI`, `SECRET_KEY`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`.
5. `python crear_admin.py`
6. `python app.py` y visita `http://127.0.0.1:5000`.
7. Entra con el administrador y agrega hoteles de prueba.

## GitHub y Render
- Verifica que `.env` no se incluya en Git (`git check-ignore .env`).
- Sube el contenido de esta carpeta a un repositorio nuevo.
- Render: Python Web Service, Build Command `pip install -r requirements.txt`, Start Command `gunicorn app:app`.
- Configura las variables `MONGO_URI` y `SECRET_KEY` en Render. El administrador debe haberse creado previamente con `crear_admin.py` contra la misma base Atlas.

## Límites de la demostración
Este proyecto NO consulta hoteles reales, NO cobra pagos y NO ofrece disponibilidad por fecha. El contador `habitaciones` es un inventario global de demostración; no equivale a un calendario de ocupación. La creación de una reserva y la cancelación actualizan dos documentos sin transacción: para uso comercial se deben implementar transacciones MongoDB, controles CSRF, validaciones adicionales, calendario de ocupación, roles y auditoría. No publicar como motor de reservaciones real sin estas mejoras.
