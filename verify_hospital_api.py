from app import app
from extensions import db
from models import User, Hospital

ctx = app.app_context()
ctx.push()
db.create_all()

user = User(name='Admin', email='admin@example.com', role='admin', department_id=None)
user.set_password('secret')
db.session.add(user)
db.session.commit()

client = app.test_client()
with client.session_transaction() as sess:
    sess['_user_id'] = str(user.id)
    sess['_fresh'] = True

resp = client.post('/api/hospitals', json={
    'name': 'Nairobi West Hospital',
    'code': 'NWH',
    'address': 'Nairobi',
    'contact_person': 'Dr. Jane',
    'phone': '0722000000',
    'email': 'info@nwh.org',
    'is_active': True,
})
print('status', resp.status_code)
print(resp.get_json())
print('exists', Hospital.query.filter_by(code='NWH').first() is not None)
ctx.pop()
