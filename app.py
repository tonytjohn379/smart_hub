from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_bcrypt import Bcrypt
from tavily import TavilyClient
from geopy.distance import geodesic
from werkzeug.utils import secure_filename
import os
from datetime import datetime
from sqlalchemy import func
import math
try:
    import qrcode
except Exception:
    qrcode = None

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///site.db'
app.config['UPLOAD_FOLDER'] = 'static/uploads'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# --- API SETUP ---
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

try:
    tavily = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None
except Exception:
    tavily = None

# --- DATABASE MODELS ---
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(60), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    
    # Provider Fields
    service_type = db.Column(db.String(50), nullable=True)
    location_name = db.Column(db.String(100), nullable=True)
    lat = db.Column(db.Float, nullable=True)
    lon = db.Column(db.Float, nullable=True)
    rating = db.Column(db.Float, default=4.5)
    phone = db.Column(db.String(20), nullable=True)
    
    # Status Fields
    is_verified = db.Column(db.Boolean, default=False)
    is_available = db.Column(db.Boolean, default=True)
    verification_file = db.Column(db.String(100), nullable=True)

class Contact(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())


class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    provider_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    scheduled_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default='pending')  # pending/confirmed/cancelled/completed
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())


class Review(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    rating = db.Column(db.Integer, nullable=False)
    comment = db.Column(db.Text, nullable=True)
    reviewer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    provider_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('booking.id'), nullable=True)
    provider_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    payer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(30), default='requested')  # requested/proof_submitted/verified/rejected
    proof_filename = db.Column(db.String(200), nullable=True)
    qr_filename = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route("/contact", methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        message = request.form.get('message')
        
        if name and email and message:
            contact_msg = Contact(name=name, email=email, message=message)
            try:
                db.session.add(contact_msg)
                db.session.commit()
                flash('Thank you! Your message has been sent successfully. We will get back to you soon!', 'success')
                return redirect(url_for('contact'))
            except Exception as e:
                flash(f'Error sending message: {e}', 'danger')
        else:
            flash('Please fill in all fields.', 'warning')
    return render_template('contact.html')

# --- PUBLIC ROUTES ---
@app.route("/")
def home():
    return render_template('index.html')

@app.route("/register", methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('home'))

    services = [s[0] for s in db.session.query(User.service_type)
                .filter(User.role=='provider', User.service_type!=None)
                .distinct().all()]

    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        role = request.form.get('role')

        # 🔥 DUPLICATE CHECK
        if User.query.filter_by(email=email).first():
            flash("Email already registered.", "danger")
            return redirect(url_for("register"))

        if User.query.filter_by(username=username).first():
            flash("Username already taken.", "danger")
            return redirect(url_for("register"))

        service = request.form.get('service_type') if role == 'provider' else None
        loc_name = request.form.get('location_name') if role == 'provider' else None
        phone = request.form.get('phone') if role == 'provider' else None
        lat = request.form.get('lat') if role == 'provider' else None
        lon = request.form.get('lon') if role == 'provider' else None

        filename = None
        if role == 'provider' and 'proof_doc' in request.files:
            file = request.files['proof_doc']
            if file.filename != '':
                filename = secure_filename(file.filename)
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

        try:
            if lat and lon:
                lat = float(lat)
                lon = float(lon)
            else:
                lat = None
                lon = None

            hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
            is_verified = True if role == 'user' else False

            user = User(
                username=username,
                email=email,
                password=hashed_password,
                phone=phone,
                role=role,
                service_type=service,
                location_name=loc_name,
                lat=lat,
                lon=lon,
                is_verified=is_verified,
                verification_file=filename
            )

            db.session.add(user)
            db.session.commit()

            flash('Account created successfully! Providers must wait for admin approval.', 'success')
            return redirect(url_for('login'))

        except Exception as e:
            db.session.rollback()
            flash(f'Error creating account: {e}', 'danger')

    return render_template('register.html', services=services)

@app.route("/login", methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if user and bcrypt.check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('dashboard'))
        else:
            flash('Login Failed.', 'danger')
    return render_template('login.html')

@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for('home'))

@app.route("/dashboard", methods=['GET', 'POST'])
@login_required
def dashboard():
    if current_user.role == 'admin':
        return redirect(url_for('admin_dashboard'))
    
    # compute list of existing service types for datalist
    services = [s[0] for s in db.session.query(User.service_type).filter(User.role=='provider', User.service_type!=None).distinct().all()]

    # PROVIDER: Manage Profile
    if current_user.role == 'provider':
        if request.method == 'POST':
            current_user.is_available = 'is_available' in request.form
            current_user.location_name = request.form.get('location_name')
            db.session.commit()
            flash('Profile Updated!', 'success')
        # fetch bookings for provider and attach customer names
        raw_bookings = Booking.query.filter_by(provider_id=current_user.id).order_by(Booking.scheduled_at.desc()).all()
        active_bookings = []
        history_bookings = []
        for b in raw_bookings:
            cust = User.query.get(b.customer_id)
            info = {'id': b.id, 'customer_name': cust.username if cust else 'Customer', 'scheduled_at': b.scheduled_at, 'status': b.status}
            if b.status in ('pending', 'confirmed'):
                active_bookings.append(info)
            else:
                history_bookings.append(info)

            # attach any payment requests related to this booking (for provider view)
            # (we'll not embed payment list here; provider payments are fetched separately)

        # fetch reviews for provider and attach reviewer names
        raw_reviews = Review.query.filter_by(provider_id=current_user.id).order_by(Review.created_at.desc()).all()
        provider_reviews = []
        for r in raw_reviews:
            reviewer = User.query.get(r.reviewer_id)
            provider_reviews.append({'rating': r.rating, 'comment': r.comment, 'reviewer_name': reviewer.username if reviewer else 'User', 'created_at': r.created_at})

        # booking stats
        total_bookings = Booking.query.filter_by(provider_id=current_user.id).count()
        pending_bookings = Booking.query.filter_by(provider_id=current_user.id, status='pending').count()
        confirmed_bookings = Booking.query.filter_by(provider_id=current_user.id, status='confirmed').count()
        completed_bookings = Booking.query.filter_by(provider_id=current_user.id, status='completed').count()

        stats = {
            'total': total_bookings,
            'pending': pending_bookings,
            'confirmed': confirmed_bookings,
            'completed': completed_bookings,
            'rating': current_user.rating
        }

        # provider payments
        raw_payments = Payment.query.filter_by(provider_id=current_user.id).order_by(Payment.created_at.desc()).all()
        provider_payments = []
        for pay in raw_payments:
            payer = User.query.get(pay.payer_id)
            provider_payments.append({'id': pay.id, 'booking_id': pay.booking_id, 'payer_name': payer.username if payer else 'Customer', 'amount': pay.amount, 'status': pay.status, 'proof': pay.proof_filename, 'qr': pay.qr_filename, 'created_at': pay.created_at})

        return render_template('dashboard.html', user=current_user, bookings=active_bookings, booking_history=history_bookings, reviews=provider_reviews, stats=stats, payments=provider_payments, services=services)
    # CUSTOMER: Search
    service_filter = request.args.get('service')
    user_lat = request.args.get('lat')
    user_lon = request.args.get('lon')
    results = []
    
    if service_filter:
        query = User.query.filter_by(role='provider', service_type=service_filter, is_verified=True, is_available=True).all()
        for provider in query:
            dist = "Unknown"
            sort_val = 9999
            if user_lat and user_lon and provider.lat and provider.lon:
                try:
                    user_coords = (float(user_lat), float(user_lon))
                    provider_coords = (provider.lat, provider.lon)
                    km = geodesic(user_coords, provider_coords).km
                    dist = round(km, 2)
                    sort_val = dist
                except:
                    pass
            # fetch latest reviews for this provider
            recent_reviews = Review.query.filter_by(provider_id=provider.id).order_by(Review.created_at.desc()).limit(3).all()
            results.append({'data': provider, 'distance': dist, 'sort_val': sort_val, 'reviews': recent_reviews})
        results.sort(key=lambda x: x['sort_val'])
    
    # fetch customer's own bookings so they can see status updates
    customer_bookings = []
    if current_user.role == 'user':
        raw_cust = Booking.query.filter_by(customer_id=current_user.id).order_by(Booking.scheduled_at.desc()).all()
        for b in raw_cust:
            prov = User.query.get(b.provider_id)
            customer_bookings.append({'id': b.id, 'provider_name': prov.username if prov else 'Provider', 'scheduled_at': b.scheduled_at, 'status': b.status})

    # fetch payments for customer
    customer_payments = []
    if current_user.role == 'user':
        raw_cust_pays = Payment.query.filter_by(payer_id=current_user.id).order_by(Payment.created_at.desc()).all()
        print(f"DEBUG: customer_payments for user {current_user.id}: found {len(raw_cust_pays)} payments")
        for p in raw_cust_pays:
            prov = User.query.get(p.provider_id)
            customer_payments.append({'id': p.id, 'provider_name': prov.username if prov else 'Provider', 'amount': p.amount, 'status': p.status, 'proof': p.proof_filename, 'qr': p.qr_filename, 'booking_id': p.booking_id, 'created_at': p.created_at})

    return render_template('dashboard.html', user=current_user, results=results, customer_bookings=customer_bookings, customer_payments=customer_payments, services=services)


@app.route('/payment/request', methods=['POST'])
@login_required
def request_payment():
    # provider requests payment from customer for a booking
    print(f"DEBUG: request_payment called, user={current_user.username}, role={current_user.role}")
    if current_user.role != 'provider':
        print(f"DEBUG: User role is '{current_user.role}', not 'provider'. Redirecting.")
        flash('Only providers can request payments.', 'warning')
        return redirect(url_for('dashboard'))
    booking_id = request.form.get('booking_id')
    amount = request.form.get('amount')
    print(f"DEBUG: booking_id={booking_id}, amount={amount}")
    if not booking_id or not amount:
        flash('Booking and amount required.', 'warning')
        return redirect(url_for('dashboard'))
    try:
        b = Booking.query.get(int(booking_id))
        if not b:
            flash('Booking not found.', 'danger')
            return redirect(url_for('dashboard'))
        payer_id = int(b.customer_id)
        p = Payment(booking_id=int(b.id), provider_id=int(current_user.id), payer_id=payer_id, amount=float(amount))
        print(f"DEBUG: Creating payment: booking_id={p.booking_id}, provider_id={p.provider_id}, payer_id={p.payer_id}, amount={p.amount}")
        db.session.add(p)
        db.session.flush()
        print(f"DEBUG: Payment flushed, id={p.id}")
        # generate QR code payload (UPI-style) and save image if qrcode lib available
        try:
            if qrcode:
                provider_upi = getattr(current_user, 'email', None) or getattr(current_user, 'phone', None) or f'user{current_user.id}@example'
                upi_payload = f"upi://pay?pa={provider_upi}&pn={current_user.username}&am={p.amount}&cu=INR&tn=Booking%20{p.booking_id}"
                qr_img = qrcode.make(upi_payload)
                qr_name = f"payment_qr_{p.id}.png"
                qr_path = os.path.join(app.config['UPLOAD_FOLDER'], qr_name)
                qr_img.save(qr_path)
                p.qr_filename = qr_name
                print(f"DEBUG: QR generated and saved to {qr_path}")
            else:
                print("DEBUG: qrcode library not available; skipping QR generation")
        except Exception as qe:
            print(f"DEBUG: QR generation error: {qe}")
        db.session.commit()
        print(f"DEBUG: Payment committed")
        # debug/log
        payer = User.query.get(payer_id)
        payer_info = f"{payer.username}<{payer.email}>" if payer else str(payer_id)
        flash(f'Payment request sent to customer: {payer_info}', 'success')
    except Exception as e:
        print(f"DEBUG ERROR: {e}")
        db.session.rollback()
        flash(f'Error requesting payment: {e}', 'danger')
    return redirect(url_for('dashboard'))


# Temporary debug endpoint to inspect incoming payment request POSTs
@app.route('/payment/request_debug', methods=['POST'])
def request_payment_debug():
    print("DEBUG: /payment/request_debug called")
    try:
        print('DEBUG HEADERS:', dict(request.headers))
        print('DEBUG COOKIES:', request.cookies)
        print('DEBUG FORM:', request.form.to_dict())
        try:
            auth_info = {
                'is_authenticated': current_user.is_authenticated,
                'id': getattr(current_user, 'id', None),
                'username': getattr(current_user, 'username', None),
                'role': getattr(current_user, 'role', None)
            }
            print('DEBUG CURRENT_USER:', auth_info)
        except Exception as e:
            print('DEBUG CURRENT_USER ERROR:', e)
    except Exception as e:
        print('DEBUG request_payment_debug error:', e)
    return redirect(url_for('dashboard'))
    return redirect(url_for('dashboard'))


@app.route('/payment/upload', methods=['POST'])
@login_required
def upload_payment_proof():
    # customer uploads proof for a payment
    payment_id = request.form.get('payment_id')
    if not payment_id:
        flash('Payment id missing.', 'warning')
        return redirect(url_for('dashboard'))
    payment = Payment.query.get(int(payment_id))
    if not payment:
        flash('Payment not found.', 'danger')
        return redirect(url_for('dashboard'))
    if current_user.id != payment.payer_id and current_user.role != 'admin':
        flash('Not authorized.', 'danger')
        return redirect(url_for('dashboard'))
    if 'proof' not in request.files:
        flash('No file uploaded.', 'warning')
        return redirect(url_for('dashboard'))
    file = request.files['proof']
    if file.filename == '':
        flash('No selected file.', 'warning')
        return redirect(url_for('dashboard'))
    filename = secure_filename(file.filename)
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(save_path)
    payment.proof_filename = filename
    payment.status = 'proof_submitted'
    db.session.commit()
    flash('Payment proof uploaded. Provider will verify.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/payment/verify/<int:payment_id>', methods=['POST'])
@login_required
def verify_payment(payment_id):
    payment = Payment.query.get(payment_id)
    if not payment:
        flash('Payment not found.', 'danger')
        return redirect(url_for('dashboard'))
    # only provider who requested or admin can verify
    if current_user.id != payment.provider_id and current_user.role != 'admin':
        flash('Not authorized.', 'danger')
        return redirect(url_for('dashboard'))
    action = request.form.get('action')
    if action == 'verify':
        payment.status = 'verified'
    elif action == 'reject':
        payment.status = 'rejected'
    db.session.commit()
    flash('Payment status updated.', 'success')
    return redirect(url_for('dashboard'))
    


@app.route('/book', methods=['POST'])
@login_required
def book_provider():
    if current_user.role != 'user':
        flash('Only customers can create bookings.', 'warning')
        return redirect(url_for('dashboard'))
    provider_id = request.form.get('provider_id')
    scheduled_at = request.form.get('scheduled_at')
    if not provider_id or not scheduled_at:
        flash('Please choose a date and time.', 'warning')
        return redirect(url_for('dashboard'))
    try:
        # datetime-local input returns 'YYYY-MM-DDTHH:MM' which fromisoformat accepts
        dt = datetime.fromisoformat(scheduled_at)
        booking = Booking(customer_id=current_user.id, provider_id=int(provider_id), scheduled_at=dt)
        db.session.add(booking)
        db.session.commit()
        flash('Booking requested. Provider will confirm shortly.', 'success')
    except Exception as e:
        flash(f'Error creating booking: {e}', 'danger')
    return redirect(url_for('dashboard'))


@app.route('/review', methods=['POST'])
@login_required
def leave_review():
    if current_user.role != 'user':
        flash('Only customers can leave reviews.', 'warning')
        return redirect(url_for('dashboard'))
    provider_id = request.form.get('provider_id')
    rating = request.form.get('rating')
    comment = request.form.get('comment')
    if not provider_id or not rating:
        flash('Rating required.', 'warning')
        return redirect(url_for('dashboard'))
    try:
        r = Review(rating=int(rating), comment=comment, reviewer_id=current_user.id, provider_id=int(provider_id))
        db.session.add(r)
        db.session.commit()
        # update provider average rating
        avg = db.session.query(func.avg(Review.rating)).filter(Review.provider_id==int(provider_id)).scalar()
        provider = User.query.get(int(provider_id))
        if avg:
            provider.rating = float(round(avg, 2))
            db.session.commit()
        flash('Review submitted. Thank you!', 'success')
    except Exception as e:
        flash(f'Error submitting review: {e}', 'danger')
    return redirect(url_for('dashboard'))


@app.route('/booking/<int:booking_id>/action', methods=['POST'])
@login_required
def booking_action(booking_id):
    booking = Booking.query.get(booking_id)
    if not booking:
        flash('Booking not found.', 'danger')
        return redirect(url_for('dashboard'))
    # only provider for this booking or admin can change
    if current_user.role != 'admin' and current_user.id != booking.provider_id:
        flash('Not authorized.', 'danger')
        return redirect(url_for('dashboard'))
    action = request.form.get('action')
    if action == 'confirm':
        booking.status = 'confirmed'
    elif action == 'cancel':
        booking.status = 'cancelled'
    elif action == 'complete':
        booking.status = 'completed'
    db.session.commit()
    flash('Booking status updated.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/provider/<int:provider_id>/contact')
@login_required
def contact_provider(provider_id):
    provider = User.query.get_or_404(provider_id)
    if provider.role != 'provider':
        flash('User is not a service provider.', 'danger')
        return redirect(url_for('dashboard'))
    
    # Fetch reviews for social proof
    reviews = Review.query.filter_by(provider_id=provider.id).order_by(Review.created_at.desc()).limit(3).all()
    review_count = Review.query.filter_by(provider_id=provider.id).count()
    
    return render_template('contact_provider.html', provider=provider, reviews=reviews, review_count=review_count)

@app.route("/chatbot", methods=['GET', 'POST'])
def chatbot():
    response_text = ""
    if request.method == 'POST':
        user_query = request.form.get('query').lower()
        service_keywords = {
            'plumber': 'Plumber', 'water': 'Plumber',
            'electric': 'Electrician', 'power': 'Electrician',
            'ac': 'AC Technician', 'cool': 'AC Technician',
            'clean': 'Cleaner', 'ambulance': 'Ambulance'
        }
        matched_service = None
        local_providers = []

        for word, db_service in service_keywords.items():
            if word in user_query:
                matched_service = db_service
                local_providers = User.query.filter_by(role='provider', service_type=db_service, is_verified=True, is_available=True).all()
                break 
        
        if local_providers:
            response_text = f"<b>Found {len(local_providers)} available {matched_service}(s):</b><br>"
            for p in local_providers:
                response_text += f"👤 <b>{p.username}</b> ({p.location_name})<br>"
            response_text += "<br><i>Check Dashboard for details.</i>"
        elif matched_service and not local_providers:
            if tavily:
                try:
                    context = tavily.search(query=user_query, search_depth="basic")
                    response_text = f"<b>No local {matched_service} available.</b><br>Web Info:<br><i>{context['results'][0]['content']}</i>"
                except:
                    response_text = "No local providers found."
        else:
            if tavily:
                try:
                    context = tavily.search(query=user_query, search_depth="basic")
                    response_text = context['results'][0]['content']
                except:
                    response_text = "Connection error."

    return render_template('chatbot.html', response=response_text)

# --- AI RECOMMENDATION ENGINE ---
@app.route("/api/recommendations")
def ai_recommendations():
    """AI-powered provider recommendation engine.
    Scores providers using: distance (40%), rating (30%), availability (15%), review volume (15%).
    Query params: service (required), lat, lon (optional for distance scoring)
    """
    service = request.args.get('service')
    user_lat = request.args.get('lat', type=float)
    user_lon = request.args.get('lon', type=float)
    limit = request.args.get('limit', 5, type=int)

    if not service:
        return jsonify({'error': 'service parameter required'}), 400

    providers = User.query.filter_by(role='provider', service_type=service, is_verified=True).all()
    scored = []

    # Get max review count for normalization
    max_reviews = 1
    for p in providers:
        rc = Review.query.filter_by(provider_id=p.id).count()
        if rc > max_reviews:
            max_reviews = rc

    for p in providers:
        # --- Availability score (15%) ---
        avail_score = 1.0 if p.is_available else 0.0

        # --- Rating score (30%) ---
        rating_score = (p.rating or 0) / 5.0

        # --- Review volume score (15%) ---
        review_count = Review.query.filter_by(provider_id=p.id).count()
        volume_score = review_count / max_reviews if max_reviews > 0 else 0

        # --- Distance score (40%) ---
        dist_km = None
        dist_score = 0.5  # default when GPS unavailable
        if user_lat and user_lon and p.lat and p.lon:
            try:
                dist_km = geodesic((user_lat, user_lon), (p.lat, p.lon)).km
                # Inverse distance scoring: 1.0 at 0km, decays to ~0 at 50+km
                dist_score = max(0, 1.0 - (dist_km / 50.0))
            except Exception:
                dist_score = 0.5

        # --- Composite score ---
        composite = (
            0.40 * dist_score +
            0.30 * rating_score +
            0.15 * avail_score +
            0.15 * volume_score
        )

        # Build explanation
        reasons = []
        if dist_km is not None and dist_km < 5:
            reasons.append(f"Very close ({round(dist_km, 1)} km away)")
        elif dist_km is not None:
            reasons.append(f"{round(dist_km, 1)} km away")
        if (p.rating or 0) >= 4.0:
            reasons.append(f"Highly rated ({p.rating}⭐)")
        if review_count >= 3:
            reasons.append(f"{review_count} verified reviews")
        if p.is_available:
            reasons.append("Available now")

        scored.append({
            'id': p.id,
            'username': p.username,
            'service_type': p.service_type,
            'location_name': p.location_name or 'Unknown',
            'rating': p.rating or 0,
            'review_count': review_count,
            'distance_km': round(dist_km, 2) if dist_km is not None else None,
            'is_available': p.is_available,
            'phone': p.phone,
            'score': round(composite * 100, 1),
            'reasons': reasons,
            'score_breakdown': {
                'distance': round(dist_score * 100, 1),
                'rating': round(rating_score * 100, 1),
                'availability': round(avail_score * 100, 1),
                'review_volume': round(volume_score * 100, 1)
            }
        })

    scored.sort(key=lambda x: x['score'], reverse=True)
    return jsonify({
        'service': service,
        'total_found': len(scored),
        'recommendations': scored[:limit],
        'algorithm': 'SmartHub AI v1.0 — Weighted Composite (Distance 40%, Rating 30%, Availability 15%, Reviews 15%)'
    })


@app.route("/api/admin/stats")
@login_required
def admin_stats_api():
    """JSON stats for admin dashboard charts."""
    if current_user.role != 'admin':
        return jsonify({'error': 'unauthorized'}), 403

    # Service distribution
    service_counts = db.session.query(User.service_type, func.count(User.id)).filter(
        User.role == 'provider', User.service_type != None
    ).group_by(User.service_type).all()

    # Booking status distribution
    booking_statuses = db.session.query(Booking.status, func.count(Booking.id)).group_by(Booking.status).all()

    # Rating distribution
    rating_dist = db.session.query(
        func.round(Review.rating), func.count(Review.id)
    ).group_by(func.round(Review.rating)).all()

    # Revenue stats
    total_revenue = db.session.query(func.sum(Payment.amount)).filter(Payment.status == 'verified').scalar() or 0
    pending_revenue = db.session.query(func.sum(Payment.amount)).filter(Payment.status.in_(['requested', 'proof_submitted'])).scalar() or 0

    return jsonify({
        'service_distribution': {s: c for s, c in service_counts},
        'booking_statuses': {s: c for s, c in booking_statuses},
        'rating_distribution': {str(int(r)): c for r, c in rating_dist if r is not None},
        'revenue': {'verified': float(total_revenue), 'pending': float(pending_revenue)},
        'totals': {
            'users': User.query.filter_by(role='user').count(),
            'providers': User.query.filter_by(role='provider').count(),
            'bookings': Booking.query.count(),
            'reviews': Review.query.count(),
            'payments': Payment.query.count()
        }
    })


# --- ADMIN MODULE UPGRADE ---
@app.route("/admin")
@login_required
def admin_dashboard():
    if current_user.role != 'admin':
        return redirect(url_for('dashboard'))
    
    # 1. Fetch Data
    pending = User.query.filter_by(role='provider', is_verified=False).all()
    all_users = User.query.filter(User.role != 'admin').all()
    contacts = Contact.query.order_by(Contact.created_at.desc()).all()
    contact_count = Contact.query.count()
    
    # 2. System Performance Metrics
    total_users = User.query.filter_by(role='user').count()
    total_providers = User.query.filter_by(role='provider').count()
    verified_providers = User.query.filter_by(role='provider', is_verified=True).count()
    total_bookings = Booking.query.count()
    total_reviews = Review.query.count()
    total_revenue = db.session.query(func.sum(Payment.amount)).filter(Payment.status == 'verified').scalar() or 0

    # 3. All bookings for admin view
    all_bookings = []
    for b in Booking.query.order_by(Booking.created_at.desc()).limit(50).all():
        cust = User.query.get(b.customer_id)
        prov = User.query.get(b.provider_id)
        all_bookings.append({
            'id': b.id,
            'customer_name': cust.username if cust else 'Unknown',
            'provider_name': prov.username if prov else 'Unknown',
            'service_type': prov.service_type if prov else 'N/A',
            'scheduled_at': b.scheduled_at,
            'status': b.status,
            'created_at': b.created_at
        })

    # 4. Service distribution for charts
    service_counts = db.session.query(User.service_type, func.count(User.id)).filter(
        User.role == 'provider', User.service_type != None
    ).group_by(User.service_type).all()
    service_labels = [s for s, c in service_counts]
    service_data = [c for s, c in service_counts]

    # 5. All payments for admin
    all_payments = []
    for p in Payment.query.order_by(Payment.created_at.desc()).limit(50).all():
        payer = User.query.get(p.payer_id)
        prov = User.query.get(p.provider_id)
        all_payments.append({
            'id': p.id,
            'payer_name': payer.username if payer else 'Unknown',
            'provider_name': prov.username if prov else 'Unknown',
            'amount': p.amount,
            'status': p.status,
            'created_at': p.created_at
        })

    return render_template('admin.html', 
                           pending_providers=pending, 
                           all_users=all_users,
                           contacts=contacts,
                           contact_count=contact_count,
                           all_bookings=all_bookings,
                           all_payments=all_payments,
                           service_labels=service_labels,
                           service_data=service_data,
                           stats={
                               'users': total_users, 
                               'providers': total_providers, 
                               'verified': verified_providers,
                               'pending': len(pending),
                               'bookings': total_bookings,
                               'reviews': total_reviews,
                               'revenue': float(total_revenue)
                           })

@app.route("/approve/<int:user_id>")
@login_required
def approve_provider(user_id):
    if current_user.role == 'admin':
        user = User.query.get(user_id)
        user.is_verified = True
        db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route("/delete/<int:user_id>")
@login_required
def delete_user(user_id):
    if current_user.role == 'admin':
        user = User.query.get(user_id)
        # Remove fake/inactive account
        db.session.delete(user)
        db.session.commit()
    return redirect(url_for('admin_dashboard'))


@app.route('/contact/delete/<int:contact_id>')
@login_required
def delete_contact(contact_id):
    if current_user.role != 'admin':
        return redirect(url_for('dashboard'))
    c = Contact.query.get(contact_id)
    if c:
        db.session.delete(c)
        db.session.commit()
    return redirect(url_for('admin_dashboard'))

def create_admin():
    admin_email = os.environ.get("ADMIN_EMAIL")
    admin_password = os.environ.get("ADMIN_PASSWORD")

    print("=== ADMIN SETUP ===")
    print("ADMIN_EMAIL configured:", bool(admin_email))
    print("ADMIN_PASSWORD configured:", bool(admin_password))

    if not admin_email or not admin_password:
        print("ADMIN_EMAIL or ADMIN_PASSWORD is not configured.")
        return

    # Find admin by email OR username
    admin = User.query.filter(
        (User.email == admin_email) | (User.username == "SuperAdmin")
    ).first()

    print("Admin user found:", bool(admin))

    if admin:
        admin.email = admin_email
        admin.password = bcrypt.generate_password_hash(admin_password).decode('utf-8')
        admin.username = "SuperAdmin"
        admin.role = "admin"
        admin.is_verified = True

        db.session.commit()

        print(">>> ADMIN ACCOUNT UPDATED")

    else:
        hashed_pw = bcrypt.generate_password_hash(admin_password).decode('utf-8')

        admin = User(
            username="SuperAdmin",
            email=admin_email,
            password=hashed_pw,
            role="admin",
            is_verified=True
        )

        db.session.add(admin)
        db.session.commit()

        print(">>> ADMIN ACCOUNT CREATED")

with app.app_context():
    db.create_all()
    create_admin()

if __name__ == '__main__':
    print("--- Starting on Port 5001 ---")
    app.run(debug=True, port=5001)