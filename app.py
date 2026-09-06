from flask import Flask, request, jsonify, session, render_template, Response
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import qrcode
import io
import base64
import random
import os
from datetime import datetime, timezone, timedelta

app = Flask(__name__)
app.secret_key = 'omnipay_secure_development_key'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, 'omnipay.db')

IST = timezone(timedelta(hours=5, minutes=30))

def get_ist_time():
    return datetime.now(IST).strftime('%d %b %Y, %I:%M %p')

def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL;')
    return conn

def init_db():
    conn = get_db()
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                phone TEXT UNIQUE,
                upi_id TEXT UNIQUE,
                pin_hash TEXT,
                balance REAL DEFAULT 5000.00
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS bank_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                bank_name TEXT,
                last_four TEXT,
                balance REAL DEFAULT 15000.00
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                utr TEXT UNIQUE,
                title TEXT,
                category TEXT,
                amount REAL,
                type TEXT DEFAULT 'DEBIT',
                source_account TEXT DEFAULT 'Omni Bank Account',
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        try:
            conn.execute('ALTER TABLE transactions ADD COLUMN source_account TEXT DEFAULT "Omni Bank Account"')
        except sqlite3.OperationalError:
            pass
            
        conn.execute('''
            CREATE TABLE IF NOT EXISTS payment_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requester_id INTEGER,
                requester_name TEXT,
                payer_phone TEXT,
                amount REAL,
                note TEXT,
                status TEXT DEFAULT 'PENDING',
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
    finally:
        conn.close()

init_db()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    name = data.get('name', '').strip()
    phone = data.get('phone', '').strip()
    pin = data.get('pin', '').strip()
    
    if not all([name, phone, pin]): 
        return jsonify({'error': 'All fields are required'}), 400
        
    if not phone.isdigit() or len(phone) != 10:
        return jsonify({'error': 'Mobile number must be exactly 10 digits.'}), 400
            
    upi_id = f"{phone}@upi"
    pin_hash = generate_password_hash(pin)
    
    conn = get_db()
    try:
        conn.execute("INSERT INTO users (name, phone, upi_id, pin_hash) VALUES (?, ?, ?, ?)", (name, phone, upi_id, pin_hash))
        conn.commit()
        return jsonify({'success': 'Account created successfully! Please login.'})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Phone number is already registered.'}), 400
    finally:
        conn.close()

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    phone = data.get('phone', '').strip()
    pin = data.get('pin', '').strip()
    
    if not phone.isdigit() or len(phone) != 10:
        return jsonify({'error': 'Enter a valid 10-digit mobile number.'}), 400
            
    conn = get_db()
    try:
        user = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
        
        if user and check_password_hash(user['pin_hash'], pin):
            session['user_id'] = user['id']
            return jsonify({'success': 'Logged in successfully'})
        return jsonify({'error': 'Invalid Phone Number or PIN'}), 401
    finally:
        conn.close()

@app.route('/api/logout', methods=['POST'])
def logout():
    session.pop('user_id', None)
    return jsonify({'success': True})

@app.route('/api/user_data')
def user_data():
    if 'user_id' not in session: 
        return jsonify({'error': 'Unauthorized'}), 401
        
    conn = get_db()
    try:
        user = conn.execute("SELECT name, phone, upi_id, balance FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        
        if user is None:
            session.pop('user_id', None)
            return jsonify({'error': 'User not found, please login again'}), 401
            
        banks = conn.execute("SELECT id, bank_name, last_four, balance FROM bank_accounts WHERE user_id = ?", (session['user_id'],)).fetchall()
        txs = conn.execute("SELECT id, utr, title, category, amount, type, source_account, timestamp FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT 50", (session['user_id'],)).fetchall()
        total_cb = conn.execute("SELECT SUM(amount) FROM transactions WHERE user_id = ? AND category = 'Rewards'", (session['user_id'],)).fetchone()[0] or 0.0
        
        return jsonify({
            'user': dict(user),
            'banks': [dict(b) for b in banks],
            'transactions': [dict(t) for t in txs], 
            'total_cashback': round(total_cb, 2)
        })
    finally:
        conn.close()

@app.route('/api/add_bank', methods=['POST'])
def add_bank():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    bank_name = data.get('bank_name', '').strip()
    dc_no = data.get('dc_no', '').strip()
    pin = data.get('pin', '').strip()

    if len(dc_no) != 16 or not dc_no.isdigit():
        return jsonify({'error': 'Debit Card must be exactly 16 digits'}), 400

    conn = get_db()
    try:
        user = conn.execute("SELECT pin_hash FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        if not check_password_hash(user['pin_hash'], pin):
            return jsonify({'error': 'Incorrect UPI PIN'}), 401
            
        last_four = dc_no[-4:]
        start_balance = random.choice([15000.00, 32000.50, 8500.75, 45000.00, 112000.00, 5000.00])
        conn.execute("INSERT INTO bank_accounts (user_id, bank_name, last_four, balance) VALUES (?, ?, ?, ?)", 
                     (session['user_id'], bank_name, last_four, start_balance))
        conn.commit()
        return jsonify({'success': True, 'message': f'{bank_name} linked successfully!'})
    finally:
        conn.close()

@app.route('/api/poll')
def poll():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    last_id = int(request.args.get('last_id', 0))
    
    conn = get_db()
    try:
        user = conn.execute("SELECT phone FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        if not user:
            return jsonify({'new_txs': [], 'pending_requests': []})

        new_txs = conn.execute(
            "SELECT id, title, amount, type, category FROM transactions WHERE user_id = ? AND id > ? ORDER BY id ASC", 
            (session['user_id'], last_id)
        ).fetchall()
        
        pending_reqs = conn.execute(
            "SELECT id, requester_name, amount, note, timestamp FROM payment_requests WHERE payer_phone = ? AND status = 'PENDING' ORDER BY id DESC", 
            (user['phone'],)
        ).fetchall()

        return jsonify({
            'new_txs': [dict(t) for t in new_txs],
            'pending_requests': [dict(r) for r in pending_reqs]
        })
    finally:
        conn.close()

@app.route('/api/check_balance', methods=['POST'])
def check_balance():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    pin = data.get('pin', '').strip()
    account_id = data.get('account_id', 'omni')
    
    conn = get_db()
    try:
        user = conn.execute("SELECT balance, pin_hash FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        if not check_password_hash(user['pin_hash'], pin):
            return jsonify({'error': 'Incorrect UPI PIN'}), 401
            
        if account_id == 'omni':
            bal = user['balance']
        else:
            bank = conn.execute("SELECT balance FROM bank_accounts WHERE id = ? AND user_id = ?", (account_id, session['user_id'])).fetchone()
            if not bank: return jsonify({'error': 'Account not found'}), 404
            bal = bank['balance']
            
        return jsonify({'success': True, 'balance': bal})
    finally:
        conn.close()

@app.route('/api/change_pin', methods=['POST'])
def change_pin():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    old_pin = data.get('old_pin', '').strip()
    new_pin = data.get('new_pin', '').strip()
    
    if len(new_pin) != 4 or not new_pin.isdigit():
        return jsonify({'error': 'New PIN must be exactly 4 digits'}), 400
        
    conn = get_db()
    try:
        user = conn.execute("SELECT pin_hash FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        if not check_password_hash(user['pin_hash'], old_pin):
            return jsonify({'error': 'Incorrect current UPI PIN'}), 401
            
        conn.execute("UPDATE users SET pin_hash = ? WHERE id = ?", (generate_password_hash(new_pin), session['user_id']))
        conn.commit()
        return jsonify({'success': 'UPI PIN updated successfully'})
    finally:
        conn.close()

@app.route('/api/telecom_plans')
def telecom_plans():
    return jsonify({
        'Jio': [{'price': 239, 'validity': '28 Days', 'data': '1.5GB/Day'}, {'price': 299, 'validity': '28 Days', 'data': '2.0GB/Day'}, {'price': 666, 'validity': '84 Days', 'data': '1.5GB/Day'}],
        'Airtel': [{'price': 265, 'validity': '28 Days', 'data': '1.0GB/Day'}, {'price': 299, 'validity': '28 Days', 'data': '1.5GB/Day'}, {'price': 719, 'validity': '84 Days', 'data': '1.5GB/Day'}],
        'Vi': [{'price': 299, 'validity': '28 Days', 'data': '1.5GB/Day'}, {'price': 479, 'validity': '56 Days', 'data': '1.5GB/Day'}, {'price': 719, 'validity': '84 Days', 'data': '1.5GB/Day'}],
        'BSNL': [{'price': 153, 'validity': '28 Days', 'data': '1.0GB/Day'}, {'price': 199, 'validity': '30 Days', 'data': '2.0GB/Day'}, {'price': 398, 'validity': '30 Days', 'data': 'Unlimited'}]
    })

@app.route('/api/add_money', methods=['POST'])
def add_money():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    amount = float(data.get('amount', 0))
    pin = data.get('pin', '').strip()
    dest_account = data.get('dest_account', 'omni')
    
    if amount <= 0: return jsonify({'error': 'Enter a valid amount.'}), 400
    
    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        user = conn.execute("SELECT pin_hash FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        
        if not check_password_hash(user['pin_hash'], pin): 
            return jsonify({'error': 'Incorrect UPI PIN'}), 401
            
        if dest_account == 'omni':
            conn.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, session['user_id']))
            dest_name = "Omni Bank Account"
        else:
            bank = conn.execute("SELECT id, bank_name, last_four FROM bank_accounts WHERE id = ? AND user_id = ?", (dest_account, session['user_id'])).fetchone()
            if not bank: return jsonify({'error': 'Invalid destination account.'}), 400
            conn.execute("UPDATE bank_accounts SET balance = balance + ? WHERE id = ?", (amount, dest_account))
            dest_name = f"{bank['bank_name']} - {bank['last_four']}"
            
        utr = f"DEP{random.randint(10000000, 99999999)}"
        conn.execute("INSERT INTO transactions (user_id, utr, title, category, amount, type, source_account, timestamp) VALUES (?, ?, 'Cash Deposit', 'Topup', ?, 'CREDIT', ?, ?)", 
                     (session['user_id'], utr, amount, dest_name, get_ist_time()))
        
        conn.commit()
        return jsonify({'success': True, 'message': f'Successfully deposited ₹{amount} to {dest_name}!'})
    except sqlite3.OperationalError:
        conn.rollback()
        return jsonify({'error': 'Server busy processing another transaction.'}), 500
    finally:
        conn.close()

@app.route('/api/fetch_bill', methods=['POST'])
def fetch_bill():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    return jsonify({
        'status': 'success', 
        'consumer_name': random.choice(["Suresh Kumar", "Anita Devi", "Rameshwar Singh", "Priya Sharma", "Aditya Patel", "Neha Gupta"]),
        'operator': data.get('operator'), 
        'consumer_id': data.get('consumer_id'),
        'due_date': '15-Oct-2026', 
        'bill_amount': random.choice([640.00, 1250.00, 2480.50, 899.00, 450.00, 150.00, 8000.00])
    })

@app.route('/api/transact', methods=['POST'])
def transact():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    amount = float(data.get('amount', 0))
    pin = data.get('pin', '').strip()
    target = data.get('title', '').strip()
    category = data.get('category', '')
    source_account = data.get('source_account', 'omni')
    
    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        user = conn.execute("SELECT name, phone, balance, pin_hash FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        
        if not check_password_hash(user['pin_hash'], pin): 
            return jsonify({'error': 'Incorrect 4-Digit UPI PIN'}), 401

        if source_account == 'omni':
            payer_bal = user['balance']
            update_query = "UPDATE users SET balance = balance - ? WHERE id = ?"
            update_id = session['user_id']
            source_name = "Omni Bank Account"
        else:
            bank = conn.execute("SELECT id, bank_name, last_four, balance FROM bank_accounts WHERE id = ? AND user_id = ?", (source_account, session['user_id'])).fetchone()
            if not bank: return jsonify({'error': 'Invalid source account selected'}), 400
            payer_bal = bank['balance']
            update_query = "UPDATE bank_accounts SET balance = balance - ? WHERE id = ?"
            update_id = bank['id']
            source_name = f"{bank['bank_name']} - {bank['last_four']}"
            
        if payer_bal < amount: 
            return jsonify({'error': f'Insufficient Balance in {source_name}'}), 400
            
        is_p2p = category in ['To Mobile Number', 'To Bank / UPI ID', 'Scan & Pay']
        recipient = None
        
        if is_p2p:
            recipient = conn.execute("SELECT id, name, balance FROM users WHERE phone = ? OR upi_id = ?", (target, target)).fetchone()
            if not recipient and category in ['To Mobile Number', 'To Bank / UPI ID']:
                return jsonify({'error': 'User not found! Please check the mobile number or UPI ID.'}), 404
        
        sender_utr = f"4{random.randint(10000000000, 99999999999)}"
        cashback_percent = random.uniform(0.5, 5.0)
        cashback_amount = round(amount * (cashback_percent / 100), 2)
        
        conn.execute(update_query, (amount, update_id))
        if cashback_amount > 0:
            conn.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (cashback_amount, session['user_id']))
        
        display_target_name = recipient['name'] if recipient else target
        conn.execute("INSERT INTO transactions (user_id, utr, title, category, amount, type, source_account, timestamp) VALUES (?, ?, ?, ?, ?, 'DEBIT', ?, ?)",
                     (session['user_id'], sender_utr, display_target_name, category, amount, source_name, get_ist_time()))
                     
        if cashback_amount > 0:
            cb_utr = f"CB{random.randint(10000000, 99999999)}"
            conn.execute("INSERT INTO transactions (user_id, utr, title, category, amount, type, source_account, timestamp) VALUES (?, ?, 'Cashback Earned', 'Rewards', ?, 'CREDIT', ?, ?)",
                         (session['user_id'], cb_utr, cashback_amount, source_name, get_ist_time()))
                         
        if recipient and is_p2p:
            rec_utr = f"4{random.randint(10000000000, 99999999999)}"
            conn.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, recipient['id']))
            conn.execute("INSERT INTO transactions (user_id, utr, title, category, amount, type, source_account, timestamp) VALUES (?, ?, ?, 'Received', ?, 'CREDIT', 'Omni Bank Account', ?)",
                         (recipient['id'], rec_utr, f"From: {user['name']}", amount, get_ist_time()))
                         
        conn.commit()
        return jsonify({'success': True, 'utr': sender_utr, 'amount': amount, 'recipient': display_target_name, 'cashback': cashback_amount, 'source_name': source_name})
    except sqlite3.OperationalError:
        conn.rollback()
        return jsonify({'error': 'Server busy processing another transaction. Please try again.'}), 500
    finally:
        conn.close()

@app.route('/api/request_money', methods=['POST'])
def request_money():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    total_amount = float(data.get('total_amount', 0))
    phones = [p.strip() for p in data.get('phones', '').split(',') if p.strip()]
    note = data.get('note', 'Payment Request')
    is_split = data.get('is_split', False)

    if total_amount <= 0 or not phones: 
        return jsonify({'error': 'Enter a valid amount and at least one phone number'}), 400

    conn = get_db()
    try:
        user = conn.execute("SELECT name, phone FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        split_count = len(phones) + 1 if is_split else len(phones)
        amount_per_person = round(total_amount / split_count, 2) if is_split else total_amount

        for p in phones:
            if p == user['phone']: continue
            conn.execute('INSERT INTO payment_requests (requester_id, requester_name, payer_phone, amount, note, timestamp) VALUES (?, ?, ?, ?, ?, ?)',
                         (session['user_id'], user['name'], p, amount_per_person, note, get_ist_time()))
        conn.commit()
        return jsonify({'success': True, 'amount_per_person': amount_per_person})
    finally:
        conn.close()

@app.route('/api/respond_request', methods=['POST'])
def respond_request():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    req_id = data.get('request_id')
    action = data.get('action')
    pin = data.get('pin', '').strip()
    source_account = data.get('source_account', 'omni')

    conn = get_db()
    try:
        req = conn.execute("SELECT * FROM payment_requests WHERE id = ? AND status = 'PENDING'", (req_id,)).fetchone()
        if not req:
            return jsonify({'error': 'Request not found or settled'}), 404

        payer = conn.execute("SELECT balance, pin_hash FROM users WHERE id = ?", (session['user_id'],)).fetchone()

        if action == 'DECLINE':
            conn.execute("UPDATE payment_requests SET status = 'DECLINED' WHERE id = ?", (req_id,))
            conn.commit()
            return jsonify({'success': True, 'message': 'Request declined'})

        if not check_password_hash(payer['pin_hash'], pin): 
            return jsonify({'error': 'Incorrect UPI PIN'}), 401

        if source_account == 'omni':
            payer_bal = payer['balance']
            update_query = "UPDATE users SET balance = balance - ? WHERE id = ?"
            update_id = session['user_id']
            source_name = "Omni Bank Account"
        else:
            bank = conn.execute("SELECT id, bank_name, last_four, balance FROM bank_accounts WHERE id = ? AND user_id = ?", (source_account, session['user_id'])).fetchone()
            if not bank: return jsonify({'error': 'Invalid source account selected'}), 400
            payer_bal = bank['balance']
            update_query = "UPDATE bank_accounts SET balance = balance - ? WHERE id = ?"
            update_id = bank['id']
            source_name = f"{bank['bank_name']} - {bank['last_four']}"

        if payer_bal < req['amount']: 
            return jsonify({'error': f'Insufficient balance in {source_name}'}), 400

        utr_payer = f"4{random.randint(10000000000, 99999999999)}"
        conn.execute(update_query, (req['amount'], update_id))
        conn.execute("INSERT INTO transactions (user_id, utr, title, category, amount, type, source_account, timestamp) VALUES (?, ?, ?, 'Request Paid', ?, 'DEBIT', ?, ?)", (session['user_id'], utr_payer, f"To: {req['requester_name']}", req['amount'], source_name, get_ist_time()))

        requester = conn.execute("SELECT balance FROM users WHERE id = ?", (req['requester_id'],)).fetchone()
        if requester:
            utr_req = f"4{random.randint(10000000000, 99999999999)}"
            conn.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (req['amount'], req['requester_id']))
            conn.execute("INSERT INTO transactions (user_id, utr, title, category, amount, type, source_account, timestamp) VALUES (?, ?, ?, 'Request Received', ?, 'CREDIT', 'Omni Bank Account', ?)", (req['requester_id'], utr_req, f"From: {req['payer_phone']}", req['amount'], get_ist_time()))

        conn.execute("UPDATE payment_requests SET status = 'PAID' WHERE id = ?", (req_id,))
        conn.commit()
        return jsonify({'success': True, 'message': f"Paid ₹{req['amount']} to {req['requester_name']}"})
    finally:
        conn.close()

@app.route('/api/my_qr')
def my_qr():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db()
    try:
        user = conn.execute("SELECT name, upi_id FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        if not user: return jsonify({'error': 'User not found'}), 404
        
        upi_uri = f"upi://pay?pa={user['upi_id']}&pn={user['name']}&cu=INR"
        qr = qrcode.make(upi_uri)
        img_io = io.BytesIO()
        qr.save(img_io, 'PNG')
        img_io.seek(0)
        qr_base64 = base64.b64encode(img_io.getvalue()).decode('utf-8')
        return jsonify({'success': True, 'qr_image': f"data:image/png;base64,{qr_base64}", 'upi_id': user['upi_id'], 'name': user['name']})
    except Exception as e:
        return jsonify({'error': f'Failed to generate QR: {str(e)}'}), 500
    finally:
        conn.close()

@app.route('/api/download_statement')
def download_statement():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    
    conn = get_db()
    try:
        txs = conn.execute("SELECT utr, title, category, amount, type, source_account, timestamp FROM transactions WHERE user_id = ? ORDER BY id DESC", (session['user_id'],)).fetchall()
        
        filtered_txs = []
        for t in txs:
            try:
                tx_date = datetime.strptime(t['timestamp'], '%d %b %Y, %I:%M %p').date()
                if start_date and tx_date < datetime.strptime(start_date, '%Y-%m-%d').date():
                    continue
                if end_date and tx_date > datetime.strptime(end_date, '%Y-%m-%d').date():
                    continue
                filtered_txs.append(t)
            except Exception:
                filtered_txs.append(t) 

        def generate():
            yield 'Date,UTR,Description,Category,Account Used,Type,Amount (INR)\n'
            for t in filtered_txs:
                source = t['source_account'] if t['source_account'] else 'Omni Bank Account'
                yield f"\"{t['timestamp']}\",\"{t['utr']}\",\"{t['title']}\",\"{t['category']}\",\"{source}\",\"{t['type']}\",{t['amount']}\n"
        
        return Response(generate(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=OmniPay_Statement.csv'})
    finally:
        conn.close()

if __name__ == '__main__':
    app.run(debug=True, port=5000)