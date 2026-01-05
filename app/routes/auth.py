"""
AUTHENTICATION ROUTES
=====================
"""

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from app.extensions import db
from app.models import User

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/')
def home():
    if current_user.is_authenticated:
        return redirect(url_for('auth.dashboard'))
    return render_template('home.html')


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

        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash('Email already registered!', 'danger')
            return redirect(url_for('auth.register'))

        new_user = User(name=name, email=email)
        new_user.set_password(password)

        db.session.add(new_user)
        db.session.commit()

        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('register.html')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('auth.dashboard'))

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        remember = request.form.get('remember', False)

        user = User.query.filter_by(email=email).first()

        if user and user.check_password(password):
            login_user(user, remember=remember)
            flash(f'Welcome back, {user.name}!', 'success')

            next_page = request.args.get('next')
            return redirect(next_page or url_for('auth.dashboard'))
        else:
            flash('Invalid email or password!', 'danger')

    return render_template('login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.home'))


@auth_bp.route('/dashboard')
@login_required
def dashboard():
    from app.models import LoanRequest, LoanRepayment, GroupMember, LoanStatus, RepaymentStatus

    # Get user's groups
    memberships = current_user.get_active_memberships().all()
    groups = [m.group for m in memberships]

    # Get pending votes count
    pending_votes = 0
    for membership in memberships:
        from app.models import LoanApproval
        group_loans = LoanRequest.query.filter_by(
            group_id=membership.group_id,
            status=LoanStatus.PENDING.value,
            is_active=True
        ).all()

        for loan in group_loans:
            if loan.requested_by != current_user.id:
                existing_vote = LoanApproval.query.filter_by(
                    loan_id=loan.id,
                    user_id=current_user.id
                ).first()
                if not existing_vote:
                    pending_votes += 1

    # Get pending repayment approvals (for admins)
    pending_repayment_approvals = 0
    for membership in memberships:
        if membership.role == 'admin':
            count = LoanRepayment.query.join(LoanRequest).filter(
                LoanRequest.group_id == membership.group_id,
                LoanRepayment.status == RepaymentStatus.PENDING.value
            ).count()
            pending_repayment_approvals += count

    # Get user's active loans
    active_loans = LoanRequest.query.filter(
        LoanRequest.requested_by == current_user.id,
        LoanRequest.is_active == True,
        LoanRequest.status == LoanStatus.DISBURSED.value
    ).all()

    total_outstanding = sum(loan.get_remaining_amount() for loan in active_loans)

    return render_template(
        'dashboard.html',
        groups=groups,
        pending_votes=pending_votes,
        pending_repayment_approvals=pending_repayment_approvals,
        active_loans=active_loans,
        total_outstanding=total_outstanding
    )