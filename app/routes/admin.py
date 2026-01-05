"""
ADMIN ROUTES
============

Admin-specific actions:
- Pending approvals dashboard
- Repayment approval/rejection
- Wallet audit
"""

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.models import (
    Group, GroupMember, LoanRequest, LoanRepayment,
    LoanStatus, RepaymentStatus, MemberRole
)
from app.services.authorization_service import is_group_admin

admin_bp = Blueprint('admin', __name__)


# ============== ADMIN DASHBOARD ==============
@admin_bp.route('/groups/<int:group_id>/admin')
@login_required
def admin_dashboard(group_id):
    group = Group.query.get_or_404(group_id)

    if not is_group_admin(current_user.id, group_id):
        flash('Admin access required!', 'danger')
        return redirect(url_for('groups.view_group', group_id=group_id))

    # Pending loan approvals (waiting for votes)
    pending_loans = LoanRequest.query.filter_by(
        group_id=group_id,
        status=LoanStatus.PENDING.value,
        is_active=True
    ).all()

    # Approved loans awaiting disbursement
    awaiting_disbursement = LoanRequest.query.filter_by(
        group_id=group_id,
        status=LoanStatus.APPROVED.value,
        is_active=True
    ).filter(LoanRequest.disbursed_at.is_(None)).all()

    # Pending repayment approvals
    pending_repayments = LoanRepayment.query.join(LoanRequest).filter(
        LoanRequest.group_id == group_id,
        LoanRepayment.status == RepaymentStatus.PENDING.value
    ).all()

    # Active loans (disbursed, not completed)
    active_loans = LoanRequest.query.filter_by(
        group_id=group_id,
        status=LoanStatus.DISBURSED.value,
        is_active=True
    ).all()

    # Member count
    member_count = GroupMember.query.filter_by(
        group_id=group_id,
        is_active=True
    ).count()

    # Other admins
    admins = GroupMember.query.filter_by(
        group_id=group_id,
        role=MemberRole.ADMIN.value,
        is_active=True
    ).all()

    return render_template(
        'admin/dashboard.html',
        group=group,
        pending_loans=pending_loans,
        awaiting_disbursement=awaiting_disbursement,
        pending_repayments=pending_repayments,
        active_loans=active_loans,
        member_count=member_count,
        admins=admins
    )


# ============== PENDING REPAYMENTS ==============
@admin_bp.route('/groups/<int:group_id>/admin/repayments')
@login_required
def pending_repayments(group_id):
    group = Group.query.get_or_404(group_id)

    if not is_group_admin(current_user.id, group_id):
        flash('Admin access required!', 'danger')
        return redirect(url_for('groups.view_group', group_id=group_id))

    # Get all pending repayments with loan details
    repayments = LoanRepayment.query.join(LoanRequest).filter(
        LoanRequest.group_id == group_id,
        LoanRepayment.status == RepaymentStatus.PENDING.value
    ).order_by(LoanRepayment.submitted_at.asc()).all()

    return render_template(
        'admin/pending_repayments.html',
        group=group,
        repayments=repayments
    )


# ============== REPAYMENT DETAIL (for approval) ==============
@admin_bp.route('/admin/repayments/<int:repayment_id>')
@login_required
def repayment_detail(repayment_id):
    repayment = LoanRepayment.query.get_or_404(repayment_id)
    loan = repayment.loan
    group = loan.group

    if not is_group_admin(current_user.id, group.id):
        flash('Admin access required!', 'danger')
        return redirect(url_for('loans.view_loan', loan_id=loan.id))

    return render_template(
        'admin/repayment_detail.html',
        repayment=repayment,
        loan=loan,
        group=group
    )


# ============== ADMIN TRANSFER HISTORY ==============
@admin_bp.route('/groups/<int:group_id>/admin/transfer-history')
@login_required
def transfer_history(group_id):
    group = Group.query.get_or_404(group_id)

    if not is_group_admin(current_user.id, group_id):
        flash('Admin access required!', 'danger')
        return redirect(url_for('groups.view_group', group_id=group_id))

    from app.models import AdminTransferHistory

    transfers = AdminTransferHistory.query.filter_by(
        group_id=group_id
    ).order_by(AdminTransferHistory.transferred_at.desc()).all()

    return render_template(
        'admin/transfer_history.html',
        group=group,
        transfers=transfers
    )