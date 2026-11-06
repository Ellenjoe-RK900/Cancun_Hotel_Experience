
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, date, timedelta
from functools import wraps

from bson import ObjectId
from flask import (
    Flask, abort, flash, jsonify, redirect,
    render_template, request, session, url_for
)
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv


# CONFIGURACION
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")

if not app.secret_key:
    raise RuntimeError("Configura SECRET_KEY en .env o Render")

uri = os.getenv("MONGO_URI")
if not uri:
    raise RuntimeError("Configura MONGO_URI en .env o Render")

client = MongoClient(uri, serverSelectionTimeoutMS=10000)
db = client["cancun_hotel_experience"]

hoteles = db["hoteles"]
usuarios = db["usuarios"]
reservas = db["reservas"]

usuarios.create_index("correo", unique=True)
reservas.create_index([
    ("hotel_id", 1),
    ("estado", 1),
    ("entrada", 1),
    ("salida", 1)
])

DURACION_RESERVA = timedelta(minutes=5)


# FUNCIONES GENERALES

def ahora():
    return datetime.now(timezone.utc)


@app.template_filter("mxn")
def mxn(value):
    return f"${float(value):,.2f} MXN"


def oid(value):
    try:
        return ObjectId(value)
    except Exception:
        abort(404)


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("uid"):
            flash("Inicia sesión para continuar.", "warning")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("uid") or not session.get("admin"):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


# RESERVAS TEMPORALES

def vencer_reservas():
    """Marca las reservas temporales cuyo plazo terminó."""
    reservas.update_many(
        {
            "estado": "temporal",
            "vence_en": {"$lte": ahora()}
        },
        {
            "$set": {
                "estado": "vencida"
            }
        }
    )


@contextmanager
def bloquear_hotel(hotel_id):
    """
    Bloqueo temporal en MongoDB para serializar
    las operaciones de reserva de un mismo hotel.
    """
    token = uuid.uuid4().hex
    adquirido = False

    for intento in range(40):
        instante = ahora()

        hotel = hoteles.find_one_and_update(
            {
                "_id": hotel_id,
                "$or": [
                    {"reserva_lock_hasta": {"$exists": False}},
                    {"reserva_lock_hasta": {"$lte": instante}}
                ]
            },
            {
                "$set": {
                    "reserva_lock_token": token,
                    "reserva_lock_hasta": instante + timedelta(seconds=30)
                }
            },
            return_document=ReturnDocument.AFTER
        )

        if hotel:
            adquirido = True
            break

        if not hoteles.find_one({"_id": hotel_id}, {"_id": 1}):
            abort(404)

        time.sleep(0.1)

    if not adquirido:
        raise RuntimeError(
            "El sistema está procesando otra reserva. Intenta nuevamente."
        )

    try:
        yield
    finally:
        hoteles.update_one(
            {
                "_id": hotel_id,
                "reserva_lock_token": token
            },
            {
                "$unset": {
                    "reserva_lock_token": "",
                    "reserva_lock_hasta": ""
                }
            }
        )


def capacidad_hotel(hotel_id):
    """
    Inicializa la capacidad total conservando
    las reservas confirmadas del sistema anterior.
    Se debe ejecutar con el bloqueo del hotel.
    """
    hotel = hoteles.find_one({"_id": hotel_id})

    if not hotel:
        abort(404)

    if "capacidad_total" not in hotel:
        anteriores = reservas.count_documents({
            "hotel_id": hotel_id,
            "estado": "confirmada"
        })

        capacidad = int(hotel.get("habitaciones", 0)) + anteriores

        hoteles.update_one(
            {
                "_id": hotel_id,
                "capacidad_total": {"$exists": False}
            },
            {"$set": {"capacidad_total": capacidad}}
        )

        hotel = hoteles.find_one({"_id": hotel_id})

    return int(hotel["capacidad_total"])


def habitaciones_ocupadas(hotel_id, entrada, salida):
    """
    Cuenta las habitaciones que coinciden con
    cualquier parte del intervalo solicitado.
    La fecha de salida no ocupa otra noche.
    """
    return reservas.count_documents({
        "hotel_id": hotel_id,
        "entrada": {"$lt": salida},
        "salida": {"$gt": entrada},
        "$or": [
            {"estado": "confirmada"},
            {
                "estado": "temporal",
                "vence_en": {"$gt": ahora()}
            }
        ]
    })


def hotel_data(form):
    nombre = form.get("nombre", "").strip()
    zona = form.get("zona", "").strip()
    descripcion = form.get("descripcion", "").strip()
    imagen = form.get("imagen", "").strip()

    if not nombre or not zona or len(nombre) > 100 or len(zona) > 100:
        raise ValueError(
            "Nombre y zona son obligatorios (máximo 100 caracteres)."
        )

    if len(descripcion) > 1500:
        raise ValueError("Descripción demasiado larga.")

    if imagen and not re.match(r"^https://", imagen):
        raise ValueError("La imagen debe ser una URL HTTPS.")

    try:
        precio = float(form.get("precio", ""))
        stock = int(form.get("habitaciones", ""))
    except (TypeError, ValueError):
        raise ValueError("Precio y habitaciones deben ser números válidos.")

    if not 0 < precio <= 1000000 or not 0 <= stock <= 10000:
        raise ValueError("Precio o habitaciones fuera de rango.")

    return {
        "nombre": nombre,
        "zona": zona,
        "descripcion": descripcion,
        "imagen": imagen,
        "precio": round(precio, 2),
        "habitaciones": stock,
        "capacidad_total": stock
    }


def serialize_hotel(h):
    return {
        "id": str(h["_id"]),
        "nombre": h["nombre"],
        "zona": h["zona"],
        "descripcion": h.get("descripcion", ""),
        "imagen": h.get("imagen", ""),
        "precio": h["precio"],
        "habitaciones": h.get(
            "capacidad_total", h.get("habitaciones", 0)
        )
    }


# PAGINA PRINCIPAL

@app.route("/")
def index():
    q = request.args.get("q", "").strip()

    filtro = {
        "$or": [
            {"nombre": {"$regex": re.escape(q), "$options": "i"}},
            {"zona": {"$regex": re.escape(q), "$options": "i"}}
        ]
    } if q else {}

    return render_template(
        "index.html",
        hoteles=list(hoteles.find(filtro).sort("nombre", 1)),
        q=q
    )


@app.route("/hotel/<hotel_id>")
def detalle(hotel_id):
    hotel = hoteles.find_one({"_id": oid(hotel_id)})

    if not hotel:
        abort(404)

    return render_template("detalle.html", hotel=hotel)


# REGISTRO Y ACCESO

@app.route("/registro", methods=["GET", "POST"])
def registro():
    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        correo = request.form.get("correo", "").strip().lower()
        clave = request.form.get("clave", "")

        valido = (
            nombre
            and len(nombre) <= 100
            and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", correo)
            and len(clave) >= 8
        )

        if not valido:
            flash(
                "Revisa tus datos. La contraseña debe tener mínimo 8 caracteres.",
                "danger"
            )

        elif usuarios.find_one({"correo": correo}):
            flash("Este correo ya está registrado.", "danger")

        else:
            usuarios.insert_one({
                "nombre": nombre,
                "correo": correo,
                "clave_hash": generate_password_hash(clave),
                "admin": False
            })

            flash("Cuenta creada. Ya puedes iniciar sesión.", "success")
            return redirect(url_for("login"))

    return render_template("registro.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        correo = request.form.get("correo", "").strip().lower()
        usuario = usuarios.find_one({"correo": correo})

        if usuario and check_password_hash(
            usuario["clave_hash"],
            request.form.get("clave", "")
        ):
            session.clear()
            session.update(
                uid=str(usuario["_id"]),
                nombre=usuario["nombre"],
                admin=bool(usuario.get("admin"))
            )
            return redirect(url_for("index"))

        flash("Credenciales incorrectas.", "danger")

    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ADMINISTRACION DE HOTELES

@app.route("/admin")
@admin_required
def admin():
    return render_template(
        "admin.html",
        hoteles=list(hoteles.find().sort("nombre", 1))
    )


@app.route("/admin/hotel/nuevo", methods=["GET", "POST"])
@admin_required
def nuevo():
    if request.method == "POST":
        try:
            hoteles.insert_one(hotel_data(request.form))
            flash("Hotel agregado.", "success")
            return redirect(url_for("admin"))
        except ValueError as e:
            flash(str(e), "danger")

    return render_template("formulario.html", hotel=None)


@app.route("/admin/hotel/<hotel_id>/editar", methods=["GET", "POST"])
@admin_required
def editar(hotel_id):
    identificador = oid(hotel_id)
    hotel = hoteles.find_one({"_id": identificador})

    if not hotel:
        abort(404)

    if request.method == "POST":
        try:
            data = hotel_data(request.form)

            with bloquear_hotel(identificador):
                vencer_reservas()

                if reservas.count_documents({
                    "hotel_id": identificador,
                    "estado": {"$in": ["confirmada", "temporal"]}
                }) > 0:
                    data.pop("habitaciones", None)
                    data.pop("capacidad_total", None)

                hoteles.update_one(
                    {"_id": identificador},
                    {"$set": data}
                )

            flash("Hotel actualizado.", "success")
            return redirect(url_for("admin"))

        except ValueError as e:
            flash(str(e), "danger")
        except RuntimeError as e:
            flash(str(e), "warning")

    return render_template("formulario.html", hotel=hotel)


@app.post("/admin/hotel/<hotel_id>/eliminar")
@admin_required
def eliminar(hotel_id):
    identificador = oid(hotel_id)

    with bloquear_hotel(identificador):
        if reservas.count_documents({"hotel_id": identificador}) > 0:
            flash(
                "No se puede eliminar un hotel con historial de reservas.",
                "warning"
            )
        else:
            hoteles.delete_one({"_id": identificador})
            flash("Hotel eliminado.", "success")

    return redirect(url_for("admin"))


# CREAR RESERVA TEMPORAL

@app.post("/hotel/<hotel_id>/reservar")
@login_required
def reservar(hotel_id):
    identificador = oid(hotel_id)

    try:
        entrada = date.fromisoformat(request.form.get("entrada", ""))
        salida = date.fromisoformat(request.form.get("salida", ""))
        huespedes = int(request.form.get("huespedes", "1"))

        if (
            entrada < date.today()
            or salida <= entrada
            or (salida - entrada).days > 30
            or not 1 <= huespedes <= 8
        ):
            raise ValueError

    except (ValueError, TypeError):
        flash(
            "Selecciona fechas válidas (máximo 30 noches) y de 1 a 8 huéspedes.",
            "danger"
        )
        return redirect(url_for("detalle", hotel_id=hotel_id))

    try:
        with bloquear_hotel(identificador):
            vencer_reservas()

            capacidad = capacidad_hotel(identificador)
            ocupadas = habitaciones_ocupadas(
                identificador,
                entrada.isoformat(),
                salida.isoformat()
            )

            if ocupadas >= capacidad:
                flash(
                    "No hay habitaciones disponibles para esas fechas.",
                    "warning"
                )
                return redirect(url_for("detalle", hotel_id=hotel_id))

            hotel = hoteles.find_one({"_id": identificador})
            instante = ahora()

            reserva = {
                "hotel_id": identificador,
                "usuario_id": oid(session["uid"]),
                "hotel_nombre": hotel["nombre"],
                "entrada": entrada.isoformat(),
                "salida": salida.isoformat(),
                "huespedes": huespedes,
                "noches": (salida - entrada).days,
                "total": round(
                    hotel["precio"] * (salida - entrada).days, 2
                ),
                "cantidad": 1,
                "estado": "temporal",
                "creado_en": instante,
                "vence_en": instante + DURACION_RESERVA
            }

            reservas.insert_one(reserva)

    except RuntimeError as e:
        flash(str(e), "warning")
        return redirect(url_for("detalle", hotel_id=hotel_id))

    flash(
        "Habitación apartada durante 5 minutos. Confirma antes de que venza.",
        "success"
    )
    return redirect(url_for("mis_reservas"))


# CONSULTAR MIS RESERVAS

@app.route("/mis-reservas")
@login_required
def mis_reservas():
    vencer_reservas()

    docs = list(
        reservas.find({
            "usuario_id": oid(session["uid"])
        }).sort("creado_en", -1)
    )

    return render_template(
        "reservas.html",
        reservas=docs
    )


# CONFIRMAR RESERVA TEMPORAL

@app.post("/reserva/<reserva_id>/confirmar")
@login_required
def confirmar(reserva_id):
    identificador = oid(reserva_id)
    usuario_id = oid(session["uid"])

    original = reservas.find_one({
        "_id": identificador,
        "usuario_id": usuario_id
    })

    if not original:
        abort(404)

    try:
        with bloquear_hotel(original["hotel_id"]):
            instante = ahora()

            resultado = reservas.update_one(
                {
                    "_id": identificador,
                    "usuario_id": usuario_id,
                    "estado": "temporal",
                    "vence_en": {"$gt": instante}
                },
                {
                    "$set": {
                        "estado": "confirmada",
                        "confirmada_en": instante
                    }
                }
            )

            if resultado.modified_count:
                flash(
                    "¡Tu reservación de hotel fue confirmada!",
                    "success"
                )
            else:
                vencer_reservas()
                flash(
                    "No se pudo confirmar. La reserva pudo haber vencido.",
                    "warning"
                )

    except RuntimeError as e:
        flash(str(e), "warning")

    return redirect(url_for("mis_reservas"))


# CANCELAR RESERVA

@app.post("/reserva/<reserva_id>/cancelar")
@login_required
def cancelar(reserva_id):
    identificador = oid(reserva_id)
    usuario_id = oid(session["uid"])

    original = reservas.find_one({
        "_id": identificador,
        "usuario_id": usuario_id
    })

    if not original:
        abort(404)

    try:
        with bloquear_hotel(original["hotel_id"]):
            vencer_reservas()

            resultado = reservas.update_one(
                {
                    "_id": identificador,
                    "usuario_id": usuario_id,
                    "estado": {"$in": ["temporal", "confirmada"]}
                },
                {
                    "$set": {
                        "estado": "cancelada",
                        "cancelada_en": ahora()
                    }
                }
            )

            if resultado.modified_count:
                flash(
                    "Reserva cancelada. La habitación volvió a estar disponible.",
                    "success"
                )
            else:
                flash(
                    "Esta reserva ya no se puede cancelar.",
                    "warning"
                )

    except RuntimeError as e:
        flash(str(e), "warning")

    return redirect(url_for("mis_reservas"))


# API

@app.get("/api/hoteles")
def api_hoteles():
    return jsonify([
        serialize_hotel(h)
        for h in hoteles.find()
    ])


@app.get("/api/hoteles/<hotel_id>")
def api_hotel(hotel_id):
    hotel = hoteles.find_one({"_id": oid(hotel_id)})

    if not hotel:
        return jsonify({"error": "No encontrado"}), 404

    return jsonify(serialize_hotel(hotel))


# ERRORES

@app.errorhandler(403)
@app.errorhandler(404)
def error(e):
    return render_template(
        "error.html",
        mensaje=str(e)
    ), e.code


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1")
