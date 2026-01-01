from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from app.extensions import db
from app.models import User

auth_bp = Blueprint('auth', __name__)


# ============== HOME PAGE ==============
@auth_bp.route('/')
def home():
    if current_user.is_authenticated:
        return redirect(url_for('auth.dashboard'))
    return render_template('home.html')


# ============== REGISTER ==============
@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('auth.dashboard'))

    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        # Validation
        if not name or not email or not password:
            flash('All fields are required!', 'danger')
            return redirect(url_for('auth.register'))

        if password != confirm_password:
            flash('Passwords do not match!', 'danger')
            return redirect(url_for('auth.register'))

        if len(password) < 6:
            flash('Password must be at least 6 characters!', 'danger')
            return redirect(url_for('auth.register'))

        # Check if email already exists
        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash('Email already registered!', 'danger')
            return redirect(url_for('auth.register'))

        # Create new user
        new_user = User(name=name, email=email)
        new_user.set_password(password)

        db.session.add(new_user)
        db.session.commit()

        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('register.html')


# ============== LOGIN ==============
@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('auth.dashboard'))

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        remember = request.form.get('remember', False)

        # Find user
        user = User.query.filter_by(email=email).first()

        # Check password
        if user and user.check_password(password):
            login_user(user, remember=remember)
            flash(f'Welcome back, {user.name}!', 'success')

            next_page = request.args.get('next')
            return redirect(next_page or url_for('auth.dashboard'))
        else:
            flash('Invalid email or password!', 'danger')

    return render_template('login.html')


# ============== LOGOUT ==============
@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.home'))


# ============== DASHBOARD ==============
@auth_bp.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')