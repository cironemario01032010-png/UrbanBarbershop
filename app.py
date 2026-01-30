from flask import Flask, json, render_template, request, session, jsonify, flash, redirect, url_for, Blueprint
from flask_session import Session
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
import psycopg2.extras
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta, time as dtime
import os
import stripe
from config import STRIPE_PUBLIC_KEY, STRIPE_SECRET_KEY
from psycopg2.extras import Json

# la chiave segreta di cicciariell va inserita qui
stripe.api_key = STRIPE_SECRET_KEY

# ======================
# CONFIG
# ======================

app = Flask(__name__)
app.secret_key = "supersecretkey"

# CONFIGURAZIONE SESSIONE - CRITICA PER IL CARRELLO
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_USE_SIGNER"] = True
app.config["SESSION_KEY_PREFIX"] = "barber_"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)

# Crea cartella per le sessioni se non esiste
SESSION_FOLDER = os.path.join(os.path.dirname(__file__), 'flask_session')
os.makedirs(SESSION_FOLDER, exist_ok=True)
app.config["SESSION_FILE_DIR"] = SESSION_FOLDER

Session(app)

# ======================
# DATABASE
# ======================

conn = psycopg2.connect(
    dbname="prenotazioni_db",
    user="postgres",
    password="1234",
    host="localhost",
    port=5432
)

cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

# ======================
# TABELLE
# ======================

# Utenti
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(10) NOT NULL CHECK (role IN ('admin','user'))
);
""")

# Servizi
cursor.execute("""
CREATE TABLE IF NOT EXISTS services (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    duration INTEGER NOT NULL,
    price NUMERIC NOT NULL
);
""")

# Extra (opzionali)
cursor.execute("""
CREATE TABLE IF NOT EXISTS extras (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    duration INTEGER NOT NULL,
    price NUMERIC NOT NULL
);
""")

# Prenotazioni
cursor.execute("""
CREATE TABLE IF NOT EXISTS bookings (
    id SERIAL PRIMARY KEY,
    user_id INT REFERENCES users(id) ON DELETE CASCADE,
    service_id INT REFERENCES services(id),
    booking_date DATE NOT NULL,
    booking_time TIME NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',
    customer_name VARCHAR(100) NOT NULL,
    customer_email VARCHAR(100) NOT NULL,
    stripe_session_id VARCHAR(255),
    created_at TIMESTAMP DEFAULT NOW()
);
""")

# Tabella di join per extras
cursor.execute("""
CREATE TABLE IF NOT EXISTS booking_extras (
    booking_id INT REFERENCES bookings(id) ON DELETE CASCADE,
    extra_id INT REFERENCES extras(id),
    quantity INT NOT NULL,
    PRIMARY KEY (booking_id, extra_id)
);
""")

# Indice unico sugli slot (uno stesso servizio nello stesso orario)
cursor.execute("""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes 
        WHERE schemaname='public' AND indexname='unique_booking_slot'
    ) THEN
        CREATE UNIQUE INDEX unique_booking_slot
        ON bookings (booking_date, booking_time, service_id);
    END IF;
END
$$;
""")

# Ordini (per prodotti comprati) con indirizzo
cursor.execute("""
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    customer_name VARCHAR(100) NOT NULL,
    customer_email VARCHAR(100) NOT NULL,
    shipping_address VARCHAR(255) NOT NULL,
    shipping_city VARCHAR(100) NOT NULL,
    shipping_zip VARCHAR(20) NOT NULL,
    shipping_country VARCHAR(50) NOT NULL,
    items JSONB NOT NULL, -- lista di prodotti e quantità
    total_price NUMERIC NOT NULL,
    stripe_session_id VARCHAR(255),
    status VARCHAR(20) DEFAULT 'pending', -- pending, paid, shipped, cancelled
    created_at TIMESTAMP DEFAULT NOW()
);
""")


conn.commit()

app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://postgres:1234@127.0.0.1:5432/prenotazioni_db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ======================
# PAGINE
# ======================


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/capelli")
def capelli():
    return render_template("capelli.html")


@app.route("/barba")
def barba():
    return render_template("barba.html")


@app.route("/skin_care")
def skin_care():
    return render_template("skin_care.html")


@app.route("/carrello")
def carrello():
    cart = get_cart()
    total_items = sum(item["quantity"] for item in cart)
    total_price = round(
        sum(item["price"] * item["quantity"] for item in cart), 2)
    return render_template(
        "carrello.html",
        cart=cart,
        total_items=total_items,
        total_price=total_price,
        stripe_public_key=STRIPE_PUBLIC_KEY
    )

# ======================
# CART FUNCTIONS
# ======================


def get_cart():
    return session.get("cart", [])


def save_cart(cart):
    session["cart"] = cart
    session.modified = True


@app.route("/api/cart/add", methods=["POST"])
def add_to_cart():
    data = request.json
    product_id = str(data.get("id"))
    name = str(data.get("name"))
    price = float(data.get("price"))

    cart = get_cart()
    for item in cart:
        if item["id"] == product_id:
            item["quantity"] += 1
            save_cart(cart)
            return jsonify(cart)

    cart.append({
        "id": product_id,
        "name": name,
        "price": price,
        "quantity": 1
    })
    save_cart(cart)
    return jsonify(cart)


@app.route("/api/cart/update", methods=["POST"])
def update_cart():
    data = request.json
    product_id = str(data.get("id"))
    delta = int(data.get("delta"))
    cart = get_cart()
    for item in cart:
        if item["id"] == product_id:
            item["quantity"] += delta
            break
    cart = [item for item in cart if item["quantity"] > 0]
    save_cart(cart)
    return jsonify(cart)


@app.route("/api/cart/remove", methods=["POST"])
def remove_from_cart():
    data = request.get_json(silent=True)
    product_id = str(data.get("id"))
    cart = get_cart()
    new_cart = [item for item in cart if item["id"] != product_id]
    save_cart(new_cart)
    total_items = sum(item["quantity"] for item in new_cart)
    total_price = round(sum(item["price"] * item["quantity"]
                        for item in new_cart), 2)
    return jsonify({
        "success": True,
        "cart": new_cart,
        "total_items": total_items,
        "total_price": total_price
    })


@app.route("/api/cart")
def get_cart_api():
    return jsonify(get_cart())

# ======================
# MESSAGES
# ======================


class Message(db.Model):
    __tablename__ = "messages"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


with app.app_context():
    db.create_all()


@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name = request.form.get("name")
        email = request.form.get("email")
        message_text = request.form.get("message")
        if not name or not email or not message_text:
            flash("Compila tutti i campi", "error")
            return redirect(url_for("contact"))
        new_message = Message(name=name, email=email, message=message_text)
        db.session.add(new_message)
        db.session.commit()
        flash("Messaggio salvato con successo!", "success")
        return redirect(url_for("contact"))
    return render_template("contatti.html")

# ======================
# STRIPE CHECKOUT
# ======================


@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    cart = get_cart()
    if not cart:
        return jsonify({"error": "Carrello vuoto"}), 400

    # Legge i dati di spedizione inviati dal form
    data = request.get_json()
    customer_name = data.get("customer_name")
    customer_email = data.get("customer_email")
    customer_address = data.get("customer_address")
    customer_city = data.get("customer_city")
    customer_zip = data.get("customer_zip")
    customer_country = data.get("customer_country", "IT")  # default Italia

    if not all([customer_name, customer_email, customer_address, customer_city, customer_zip]):
        return jsonify({"error": "Compila tutti i campi di spedizione"}), 400

    line_items = []
    for item in cart:
        line_items.append({
            "price_data": {
                "currency": "eur",
                "product_data": {"name": item["name"]},
                "unit_amount": int(item["price"] * 100),
            },
            "quantity": item["quantity"],
        })

    try:
        # 1️⃣ Creo sessione Stripe
        session_stripe = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=line_items,
            mode="payment",
            success_url=url_for("success", _external=True),
            cancel_url=url_for("carrello", _external=True),
            customer_email=customer_email,
            shipping_address_collection={
                "allowed_countries": ["IT"]
            }
        )

        # 2️⃣ Salvo ordine nel DB
        cursor.execute("""
            INSERT INTO orders
            (customer_name, customer_email, shipping_address, shipping_city, shipping_zip, shipping_country, items, total_price, stripe_session_id, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')
        """, (
            customer_name,
            customer_email,
            customer_address,
            customer_city,
            customer_zip,
            customer_country,
            json.dumps(cart),
            round(sum(item["price"]*item["quantity"] for item in cart), 2),
            session_stripe.id
        ))
        conn.commit()

        return jsonify({"id": session_stripe.id})

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500


@app.route("/success")
def success():
    session.pop("cart", None)
    return render_template("success.html")

# ======================
# AUTH
# ======================


@app.route("/api/register", methods=["POST"])
@app.route("/register", methods=["POST"])
def register():
    data = request.get_json()
    username = data.get("username")
    password = data.get("password")
    role = data.get("role", "user")
    try:
        cursor.execute(
            "INSERT INTO users (username,password_hash,role) VALUES (%s,%s,%s)",
            (username, generate_password_hash(password), role)
        )
        conn.commit()
        return "Registrazione completata", 200
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return "Username già esistente", 400


@app.route("/api/login", methods=["POST"])
@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    cursor.execute("SELECT * FROM users WHERE username=%s",
                   (data.get("username"),))
    user = cursor.fetchone()
    if user and check_password_hash(user["password_hash"], data.get("password")):
        session["user_id"] = user["id"]
        session["role"] = user["role"]
        return "Login OK", 200
    return "Credenziali errate", 401


@app.route("/api/logout", methods=["POST"])
@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return "Logout OK", 200

# ======================
# BOOKINGS
# ======================


@app.route("/prenotazioni")
def prenotazioni():
    return render_template("prenotazioni.html")


@app.route("/api/bookings", methods=["GET"])
@app.route("/bookings", methods=["GET"])
def get_bookings():
    if not session.get("user_id"):
        return jsonify([])
    if session["role"] == "admin":
        cursor.execute("""
            SELECT booking_date, booking_time, u.username
            FROM bookings b
            JOIN users u ON b.user_id = u.id
        """)
        rows = cursor.fetchall()
        return jsonify([{
            "username": r["username"],
            "booking_date": str(r["booking_date"]),
            "booking_time": str(r["booking_time"])[:5]
        } for r in rows])
    cursor.execute("""
        SELECT booking_date, booking_time
        FROM bookings WHERE user_id=%s
    """, (session["user_id"],))
    rows = cursor.fetchall()
    return jsonify([{
        "booking_date": str(r["booking_date"]),
        "booking_time": str(r["booking_time"])[:5]
    } for r in rows])


@app.route("/api/bookings", methods=["POST"])
@app.route("/bookings", methods=["POST"])
def create_booking():
    if not session.get("user_id"):
        return "Non autenticato", 401

    data = request.get_json() or {}

    service_id = data.get("service_id")
    extras = data.get("extras") or []
    booking_date = data.get("booking_date") or data.get("date")
    booking_time = data.get("booking_time") or data.get("time")

    # VALIDAZIONE BASE
    if not service_id or not booking_date or not booking_time:
        return "Dati mancanti", 400

    # Controllo durata servizio
    cursor.execute(
        "SELECT duration FROM services WHERE id=%s", (service_id,))
    service = cursor.fetchone()
    if not service:
        return "Servizio non valido", 400

    service_duration = service["duration"]

    # Controllo durata extra
    extra_duration = 0
    if extras:
        extras_tuple = tuple(map(int, extras))
        cursor.execute(
            "SELECT COALESCE(SUM(duration),0) as total FROM extras WHERE id = ANY(%s)",
            (list(extras_tuple),)
        )
        extra_duration = cursor.fetchone()["total"]

    total_duration = service_duration + extra_duration

    # Controllo slot liberi
    h, m = booking_time.split(":")
    start_minutes = int(h) * 60 + int(m)
    end_minutes = start_minutes + total_duration

    cursor.execute("""
        SELECT booking_time, s.duration, b.extras
        FROM bookings b
        JOIN services s ON b.service_id = s.id
        WHERE booking_date=%s
          AND status IN ('pending','paid')
    """, (booking_date,))

    bookings = cursor.fetchall()

    for b in bookings:
        b_start = b["booking_time"].hour * 60 + b["booking_time"].minute
        b_duration = b["duration"]

        b_extra_duration = 0
        if b["extras"]:
            extras_ids = [int(x) for x in b["extras"].split(",") if x]
            if extras_ids:
                cursor.execute(
                    "SELECT COALESCE(SUM(duration),0) as total FROM extras WHERE id = ANY(%s)",
                    (extras_ids,)
                )
                b_extra_duration = cursor.fetchone()["total"]

        b_end = b_start + b_duration + b_extra_duration

        if max(start_minutes, b_start) < min(end_minutes, b_end):
            return "Slot non disponibile", 400

    # Inserimento prenotazione
    cursor.execute("""
        INSERT INTO bookings (user_id, service_id, extras, booking_date, booking_time, status)
        VALUES (%s,%s,%s,%s,%s,'pending')
    """, (
        session["user_id"],
        service_id,
        ",".join(map(str, extras)) if extras else None,
        booking_date,
        booking_time
    ))

    conn.commit()
    return "Prenotazione confermata", 200


# ======================
# API SLOT DISPONIBILI
# ======================

# ======================
# API SLOT DISPONIBILI OTTIMIZZATA
# ======================
@app.route("/api/available_slots", methods=["GET"])
def available_slots():
    """
    Restituisce tutti gli slot disponibili per un determinato servizio + extras in una data.
    Gli slot sono calcolati considerando tutte le prenotazioni esistenti e la durata totale (servizio + extras).
    """
    # ------------- 1. Parametri -------------
    service_id = request.args.get("service_id")
    extras = request.args.getlist("extras[]")  # lista vuota se non presenti
    date = request.args.get("date")

    if not service_id or not date:
        return jsonify({"error": "Parametri mancanti"}), 400

    # Verifica che service_id sia un intero
    try:
        service_id = int(service_id)
    except ValueError:
        return jsonify({"error": "ID servizio non valido"}), 400

    # ------------- 2. Durata totale -------------
    cursor.execute("SELECT duration FROM services WHERE id=%s", (service_id,))
    service_row = cursor.fetchone()
    if not service_row:
        return jsonify({"error": "Servizio non valido"}), 400

    service_duration = service_row["duration"]

    # Calcolo durata extras
    extra_duration = 0
    extras_ids = []
    if extras:
        try:
            extras_ids = [int(e) for e in extras]
            if extras_ids:
                cursor.execute(
                    "SELECT COALESCE(SUM(duration),0) AS total FROM extras WHERE id IN %s",
                    (tuple(extras_ids),)
                )
                extra_duration = cursor.fetchone()["total"] or 0
        except ValueError:
            return jsonify({"error": "ID extra non valido"}), 400

    total_duration = service_duration + extra_duration

    # ------------- 3. Tutti gli slot possibili (ogni 15 min dalle 10 alle 20) -------------
    all_slots = [f"{h:02d}:{m:02d}" for h in range(10, 20) for m in [
        0, 15, 30, 45]]

    # ------------- 4. Prenotazioni esistenti -------------
    cursor.execute("""
        SELECT b.booking_time, s.duration, b.extras
        FROM bookings b
        JOIN services s ON b.service_id = s.id
        WHERE b.booking_date=%s AND b.status IN ('pending','paid')
    """, (date,))
    bookings = cursor.fetchall()

    # Pre-calcolo durata totale delle prenotazioni esistenti
    existing_bookings = []
    for b in bookings:
        b_start = b["booking_time"].hour * 60 + b["booking_time"].minute
        b_duration = b["duration"]

        b_extra_duration = 0
        if b["extras"]:
            try:
                b_extras_ids = [int(x) for x in b["extras"].split(",") if x]
                if b_extras_ids:
                    cursor.execute(
                        "SELECT COALESCE(SUM(duration),0) AS total FROM extras WHERE id IN %s",
                        (tuple(b_extras_ids),)
                    )
                    b_extra_duration = cursor.fetchone()["total"] or 0
            except Exception:
                pass  # se formato non corretto, ignora

        b_end = b_start + b_duration + b_extra_duration
        existing_bookings.append((b_start, b_end))

    # ------------- 5. Filtra slot disponibili -------------
    available = []
    for slot in all_slots:
        start_min = int(slot.split(":")[0])*60 + int(slot.split(":")[1])
        end_min = start_min + total_duration

        conflict = False
        for b_start, b_end in existing_bookings:
            if max(start_min, b_start) < min(end_min, b_end):
                conflict = True
                break

        if not conflict:
            available.append(slot)

    return jsonify({"slots": available})


@app.route("/api/bookings/checkout", methods=["POST"])
def booking_checkout():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Dati mancanti"}), 400

    # Prendo i dati dal client
    service_name = data.get("service_name")
    service_price = data.get("service_price")
    booking_date = data.get("booking_date")
    booking_time = data.get("booking_time")
    customer_name = data.get("customer_name")
    customer_email = data.get("customer_email")
    service_id = data.get("service_id")  # necessario per controllare lo slot
    user_id = session.get("user_id")  # opzionale, None se non loggato

    # Validazioni base
    if not all([service_name, service_price, booking_date, booking_time, customer_name, customer_email, service_id]):
        return jsonify({"error": "Dati cliente mancanti o non validi"}), 400

    try:
        service_price = float(service_price)
        service_id = int(service_id)
        if service_price < 0:
            raise ValueError
    except ValueError:
        return jsonify({"error": "Prezzo o ID servizio non valido"}), 400

    try:
        # 1. Controllo se lo slot è già occupato
        cursor.execute("""
            SELECT 1 FROM bookings
            WHERE booking_date = %s
              AND booking_time = %s
              AND service_id = %s
              AND status IN ('pending', 'paid')
        """, (booking_date, booking_time, service_id))

        if cursor.fetchone():
            return jsonify({"error": "Slot già occupato"}), 400

        # 2. Inserisco la prenotazione nel DB
        cursor.execute("""
            INSERT INTO bookings 
            (user_id, service_id, booking_date, booking_time, customer_name, customer_email, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending')
            RETURNING id
        """, (user_id, service_id, booking_date, booking_time, customer_name, customer_email))
        booking_id = cursor.fetchone()[0]
        conn.commit()

        # 3. Creo sessione Stripe
        stripe_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            customer_email=customer_email,
            line_items=[{
                "price_data": {
                    "currency": "eur",
                    "product_data": {"name": service_name},
                    "unit_amount": int(service_price * 100),
                },
                "quantity": 1,
            }],
            success_url=url_for("booking_success", _external=True) +
            "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("prenotazioni", _external=True)
        )

        # 4. Aggiorno la prenotazione con stripe_session_id
        cursor.execute("""
            UPDATE bookings
            SET stripe_session_id = %s
            WHERE id = %s
        """, (stripe_session.id, booking_id))
        conn.commit()

        return jsonify({"session_id": stripe_session.id})

    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "Slot già occupato"}), 400
    except stripe.error.StripeError as e:
        conn.rollback()
        return jsonify({"error": f"Errore Stripe: {str(e)}"}), 500
    except Exception as e:
        conn.rollback()
        return jsonify({"error": f"Errore server: {str(e)}"}), 500


@app.route("/booking-success")
def booking_success():
    session_id = request.args.get("session_id")

    if not session_id:
        flash("Sessione Stripe mancante", "error")
        return redirect(url_for("prenotazioni"))

    stripe_session = stripe.checkout.Session.retrieve(session_id)

    if stripe_session.payment_status == "paid":
        cursor.execute("""
            UPDATE bookings
            SET status = 'paid'
            WHERE stripe_session_id = %s
        """, (session_id,))
        conn.commit()

        flash("Prenotazione confermata e pagata con successo!", "success")
    else:
        flash("Pagamento non completato.", "error")

    return render_template("booking_success.html")


# ======================
# AVVIO
# ======================
if __name__ == "__main__":
    app.run(debug=True)
