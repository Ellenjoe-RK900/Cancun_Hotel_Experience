import os
from dotenv import load_dotenv
from pymongo import MongoClient
from werkzeug.security import generate_password_hash
load_dotenv()
correo = os.getenv('ADMIN_EMAIL', '').strip().lower()
clave = os.getenv('ADMIN_PASSWORD', '')
if not correo or len(clave) < 12 or not os.getenv('MONGO_URI'):
    raise SystemExit('Configura MONGO_URI, ADMIN_EMAIL y ADMIN_PASSWORD (12+ caracteres) en .env')
usuarios = MongoClient(os.environ['MONGO_URI'])['cancun_hotel_experience']['usuarios']
usuarios.update_one({'correo': correo}, {'$set': {'nombre': 'Administrador', 'clave_hash': generate_password_hash(clave), 'admin': True}}, upsert=True)
print('Administrador configurado.')
