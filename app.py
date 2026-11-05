import os
import re
from datetime import datetime, timezone, date
from decimal import Decimal
from functools import wraps
from bson import ObjectId
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY')
if not app.secret_key:
    raise RuntimeError('Configura SECRET_KEY en .env o Render')
uri = os.getenv('MONGO_URI')
if not uri:
    raise RuntimeError('Configura MONGO_URI en .env o Render')
client = MongoClient(uri, serverSelectionTimeoutMS=10000)
db = client['cancun_hotel_experience']
hoteles = db['hoteles']
usuarios = db['usuarios']
reservas = db['reservas']
usuarios.create_index('correo', unique=True)
reservas.create_index([('hotel_id', 1), ('estado', 1)])

@app.template_filter('mxn')
def mxn(value):
    return f'${float(value):,.2f} MXN'

def oid(value):
    try:
        return ObjectId(value)
    except Exception:
        abort(404)

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('uid'):
            flash('Inicia sesión para continuar.', 'warning')
            return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('uid') or not session.get('admin'):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper

def hotel_data(form):
    nombre = form.get('nombre', '').strip()
    zona = form.get('zona', '').strip()
    descripcion = form.get('descripcion', '').strip()
    imagen = form.get('imagen', '').strip()
    if not nombre or not zona or len(nombre) > 100 or len(zona) > 100:
        raise ValueError('Nombre y zona son obligatorios (máximo 100 caracteres).')
    if len(descripcion) > 1500:
        raise ValueError('Descripción demasiado larga.')
    if imagen and not re.match(r'^https://', imagen):
        raise ValueError('La imagen debe ser una URL HTTPS.')
    try:
        precio = float(form.get('precio', ''))
        stock = int(form.get('habitaciones', ''))
    except (TypeError, ValueError):
        raise ValueError('Precio y habitaciones deben ser números válidos.')
    if not 0 < precio <= 1000000 or not 0 <= stock <= 10000:
        raise ValueError('Precio o habitaciones fuera de rango.')
    return {'nombre': nombre, 'zona': zona, 'descripcion': descripcion, 'imagen': imagen,
            'precio': round(precio, 2), 'habitaciones': stock}

def serialize_hotel(h):
    return {'id': str(h['_id']), 'nombre': h['nombre'], 'zona': h['zona'],
            'descripcion': h.get('descripcion', ''), 'imagen': h.get('imagen', ''),
            'precio': h['precio'], 'habitaciones': h['habitaciones']}

@app.route('/')
def index():
    q = request.args.get('q', '').strip()
    filtro = {'$or': [{'nombre': {'$regex': re.escape(q), '$options': 'i'}},
                      {'zona': {'$regex': re.escape(q), '$options': 'i'}}]} if q else {}
    return render_template('index.html', hoteles=list(hoteles.find(filtro).sort('nombre', 1)), q=q)

@app.route('/hotel/<hotel_id>')
def detalle(hotel_id):
    hotel = hoteles.find_one({'_id': oid(hotel_id)})
    if not hotel:
        abort(404)
    return render_template('detalle.html', hotel=hotel)

@app.route('/registro', methods=['GET', 'POST'])
def registro():
    if request.method == 'POST':
        nombre = request.form.get('nombre', '').strip()
        correo = request.form.get('correo', '').strip().lower()
        clave = request.form.get('clave', '')
        if not nombre or len(nombre) > 100 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', correo) or len(clave) < 8:
            flash('Revisa tus datos. La contraseña debe tener mínimo 8 caracteres.', 'danger')
        elif usuarios.find_one({'correo': correo}):
            flash('Este correo ya está registrado.', 'danger')
        else:
            usuarios.insert_one({'nombre': nombre, 'correo': correo, 'clave_hash': generate_password_hash(clave), 'admin': False})
            flash('Cuenta creada. Ya puedes iniciar sesión.', 'success')
            return redirect(url_for('login'))
    return render_template('registro.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        correo = request.form.get('correo', '').strip().lower()
        usuario = usuarios.find_one({'correo': correo})
        if usuario and check_password_hash(usuario['clave_hash'], request.form.get('clave', '')):
            session.clear()
            session.update(uid=str(usuario['_id']), nombre=usuario['nombre'], admin=bool(usuario.get('admin')))
            return redirect(url_for('index'))
        flash('Credenciales incorrectas.', 'danger')
    return render_template('login.html')

@app.post('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/admin')
@admin_required
def admin():
    return render_template('admin.html', hoteles=list(hoteles.find().sort('nombre', 1)))

@app.route('/admin/hotel/nuevo', methods=['GET', 'POST'])
@admin_required
def nuevo():
    if request.method == 'POST':
        try:
            hoteles.insert_one(hotel_data(request.form))
            flash('Hotel agregado.', 'success')
            return redirect(url_for('admin'))
        except ValueError as e:
            flash(str(e), 'danger')
    return render_template('formulario.html', hotel=None)

@app.route('/admin/hotel/<hotel_id>/editar', methods=['GET', 'POST'])
@admin_required
def editar(hotel_id):
    hotel = hoteles.find_one({'_id': oid(hotel_id)})
    if not hotel:
        abort(404)
    if request.method == 'POST':
        try:
            data = hotel_data(request.form)
            # Preserve available inventory while active reservations exist.
            if reservas.count_documents({'hotel_id': hotel['_id'], 'estado': 'confirmada'}) > 0:
                data.pop('habitaciones', None)
            hoteles.update_one({'_id': hotel['_id']}, {'$set': data})
            flash('Hotel actualizado.', 'success')
            return redirect(url_for('admin'))
        except ValueError as e:
            flash(str(e), 'danger')
    return render_template('formulario.html', hotel=hotel)

@app.post('/admin/hotel/<hotel_id>/eliminar')
@admin_required
def eliminar(hotel_id):
    identificador = oid(hotel_id)
    if reservas.count_documents({'hotel_id': identificador}) > 0:
        flash('No se puede eliminar un hotel con historial de reservas.', 'warning')
    else:
        hoteles.delete_one({'_id': identificador})
        flash('Hotel eliminado.', 'success')
    return redirect(url_for('admin'))

@app.post('/hotel/<hotel_id>/reservar')
@login_required
def reservar(hotel_id):
    hotel_identificador = oid(hotel_id)
    try:
        entrada = date.fromisoformat(request.form.get('entrada', ''))
        salida = date.fromisoformat(request.form.get('salida', ''))
        huespedes = int(request.form.get('huespedes', '1'))
        if entrada < date.today() or salida <= entrada or (salida - entrada).days > 30 or not 1 <= huespedes <= 8:
            raise ValueError
    except ValueError:
        flash('Selecciona fechas futuras válidas (máximo 30 noches) y de 1 a 8 huéspedes.', 'danger')
        return redirect(url_for('detalle', hotel_id=hotel_id))
    # A simple demonstration: inventory represents remaining reservable units globally,
    # not a real date-by-date availability engine.
    hotel = hoteles.find_one_and_update({'_id': hotel_identificador, 'habitaciones': {'$gte': 1}},
                                       {'$inc': {'habitaciones': -1}}, return_document=ReturnDocument.AFTER)
    if not hotel:
        flash('No hay habitaciones disponibles.', 'warning')
        return redirect(url_for('detalle', hotel_id=hotel_id))
    try:
        reservas.insert_one({'hotel_id': hotel_identificador, 'usuario_id': oid(session['uid']),
                            'hotel_nombre': hotel['nombre'], 'entrada': entrada.isoformat(), 'salida': salida.isoformat(),
                            'huespedes': huespedes, 'noches': (salida - entrada).days,
                            'total': round(hotel['precio'] * (salida - entrada).days, 2),
                            'estado': 'confirmada', 'creado_en': datetime.now(timezone.utc)})
    except Exception:
        hoteles.update_one({'_id': hotel_identificador}, {'$inc': {'habitaciones': 1}})
        raise
    flash('Reserva registrada. Es una simulación; no se realizó ningún cobro.', 'success')
    return redirect(url_for('mis_reservas'))

@app.route('/mis-reservas')
@login_required
def mis_reservas():
    docs = list(reservas.find({'usuario_id': oid(session['uid'])}).sort('creado_en', -1))
    return render_template('reservas.html', reservas=docs)

@app.post('/reserva/<reserva_id>/cancelar')
@login_required
def cancelar(reserva_id):
    # In production, use a MongoDB transaction for atomic cancellation + stock restoration.
    reserva = reservas.find_one_and_update({'_id': oid(reserva_id), 'usuario_id': oid(session['uid']), 'estado': 'confirmada'},
                                           {'$set': {'estado': 'cancelada', 'cancelada_en': datetime.now(timezone.utc)}},
                                           return_document=ReturnDocument.AFTER)
    if reserva:
        hoteles.update_one({'_id': reserva['hotel_id']}, {'$inc': {'habitaciones': 1}})
        flash('Reserva cancelada.', 'success')
    else:
        flash('No se pudo cancelar esta reserva.', 'warning')
    return redirect(url_for('mis_reservas'))

@app.get('/api/hoteles')
def api_hoteles():
    return jsonify([serialize_hotel(h) for h in hoteles.find()])

@app.get('/api/hoteles/<hotel_id>')
def api_hotel(hotel_id):
    h = hoteles.find_one({'_id': oid(hotel_id)})
    if not h:
        return jsonify({'error': 'No encontrado'}), 404
    return jsonify(serialize_hotel(h))

@app.errorhandler(403)
@app.errorhandler(404)
def error(e):
    return render_template('error.html', mensaje=str(e)), e.code

if __name__ == '__main__':
    app.run(debug=os.getenv('FLASK_DEBUG') == '1')
