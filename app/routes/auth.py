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
    from app.models import (
        LoanRequest, LoanRepayment, GroupMember, LoanStatus, RepaymentStatus,
        LoanApproval, MemberContribution, MemberLedger, EMISchedule, WalletTransaction,
        TransactionType
    )
    from datetime import datetime, timedelta
    from sqlalchemy import or_, and_

    # Get user's groups
    memberships = current_user.get_active_memberships().all()
    groups = [m.group for m in memberships]
    group_ids = [m.group_id for m in memberships]

    # Get pending votes count AND a specific loan for voting
    pending_votes = 0
    pending_loan_for_vote = None

    for membership in memberships:
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
                    # Get the first loan that needs user's vote
                    if not pending_loan_for_vote:
                        pending_loan_for_vote = loan

    # Get pending repayment approvals (for admins) AND a specific loan for review
    pending_repayment_approvals = 0
    pending_repayment_loan = None

    for membership in memberships:
        if membership.role == 'admin':
            # Count pending repayments
            count = LoanRepayment.query.join(LoanRequest).filter(
                LoanRequest.group_id == membership.group_id,
                LoanRepayment.status == RepaymentStatus.PENDING.value
            ).count()
            pending_repayment_approvals += count

            # Get first loan with pending repayments for admin review
            if count > 0 and not pending_repayment_loan:
                # Find a loan that has pending repayments
                loan_with_pending = LoanRequest.query.join(LoanRepayment).filter(
                    LoanRequest.group_id == membership.group_id,
                    LoanRepayment.status == RepaymentStatus.PENDING.value
                ).first()
                if loan_with_pending:
                    pending_repayment_loan = loan_with_pending

    # Get user's active loans
    active_loans = LoanRequest.query.filter(
        LoanRequest.requested_by == current_user.id,
        LoanRequest.is_active == True,
        LoanRequest.status == LoanStatus.DISBURSED.value
    ).all()

    total_outstanding = sum(loan.get_remaining_amount() for loan in active_loans)

    # ========== Total Contributions ==========
    total_contributions = db.session.query(
        db.func.coalesce(db.func.sum(MemberContribution.amount), 0)
    ).filter(
        MemberContribution.user_id == current_user.id
    ).scalar() or 0

    # ========== Total Interest Earned ==========
    total_interest_earned = db.session.query(
        db.func.coalesce(db.func.sum(MemberLedger.interest_earned), 0)
    ).filter(
        MemberLedger.user_id == current_user.id
    ).scalar() or 0

    # ========== Next Due Payment (Reminder: show if due within 6 days) ==========
    next_emi = None
    if active_loans:
        loan_ids = [loan.id for loan in active_loans]
        today = datetime.utcnow().date()
        # This is the "deadline" for the reminder
        reminder_window_end = today + timedelta(days=6)

        next_emi = EMISchedule.query.filter(
            EMISchedule.loan_id.in_(loan_ids),
            EMISchedule.is_paid == False,
            EMISchedule.due_date >= today,  # Must be today or in the future
            EMISchedule.due_date <= reminder_window_end  # AND must be within the next 6 days
        ).order_by(EMISchedule.due_date.asc()).first()

    # ========== Recent Activities ==========
    recent_activities = []

    # Recent contributions by user
    recent_contributions = MemberContribution.query.filter(
        MemberContribution.user_id == current_user.id
    ).order_by(MemberContribution.contributed_at.desc()).limit(3).all()

    for contrib in recent_contributions:
        recent_activities.append({
            'message': f'Contributed ₹{contrib.amount:.0f} to {contrib.wallet.group.name}',
            'icon': 'bi-plus-circle',
            'color': 'success',
            'timestamp': contrib.contributed_at
        })

    # Recent repayments by user
    recent_repayments = LoanRepayment.query.filter(
        LoanRepayment.paid_by == current_user.id,
        LoanRepayment.status == RepaymentStatus.APPROVED.value
    ).order_by(LoanRepayment.approved_at.desc()).limit(3).all()

    for repay in recent_repayments:
        recent_activities.append({
            'message': f'Repaid ₹{repay.amount:.0f} for loan',
            'icon': 'bi-cash',
            'color': 'info',
            'timestamp': repay.approved_at
        })

    # Recent loan status changes for user's loans
    recent_loan_updates = LoanRequest.query.filter(
        LoanRequest.requested_by == current_user.id,
        LoanRequest.is_active == True,
        LoanRequest.status.in_([LoanStatus.APPROVED.value, LoanStatus.DISBURSED.value, LoanStatus.REJECTED.value])
    ).order_by(LoanRequest.updated_at.desc()).limit(3).all()

    for loan in recent_loan_updates:
        if loan.status == LoanStatus.DISBURSED.value:
            recent_activities.append({
                'message': f'Loan ₹{loan.approved_amount:.0f} disbursed',
                'icon': 'bi-check-circle',
                'color': 'success',
                'timestamp': loan.disbursed_at
            })
        elif loan.status == LoanStatus.APPROVED.value:
            recent_activities.append({
                'message': f'Loan ₹{loan.amount:.0f} approved',
                'icon': 'bi-hand-thumbs-up',
                'color': 'primary',
                'timestamp': loan.approved_at
            })
        elif loan.status == LoanStatus.REJECTED.value:
            recent_activities.append({
                'message': f'Loan ₹{loan.amount:.0f} rejected',
                'icon': 'bi-x-circle',
                'color': 'danger',
                'timestamp': loan.rejected_at
            })

    # Sort activities by timestamp and take latest 5
    recent_activities = sorted(
        [a for a in recent_activities if a['timestamp']],
        key=lambda x: x['timestamp'],
        reverse=True
    )[:5]

    # Add time_ago to activities
    def time_ago(dt):
        if not dt:
            return ''
        now = datetime.utcnow()
        diff = now - dt
        if diff.days > 0:
            return f'{diff.days}d ago'
        elif diff.seconds >= 3600:
            return f'{diff.seconds // 3600}h ago'
        elif diff.seconds >= 60:
            return f'{diff.seconds // 60}m ago'
        else:
            return 'Just now'

    for activity in recent_activities:
        activity['time_ago'] = time_ago(activity['timestamp'])

    return render_template(
        'dashboard.html',
        groups=groups,
        pending_votes=pending_votes,
        pending_repayment_approvals=pending_repayment_approvals,
        pending_loan_for_vote=pending_loan_for_vote,
        pending_repayment_loan=pending_repayment_loan,
        active_loans=active_loans,
        total_outstanding=total_outstanding,
        total_contributions=total_contributions,
        total_interest_earned=total_interest_earned,
        next_emi=next_emi,
        recent_activities=recent_activities,
        now=datetime.utcnow()
    )
