"""
LOAN ROUTES
===========

Uses loan_service for all operations.
Implements strict state machine.
"""
from datetime import datetime

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.extensions import db
from app.models import (
    Group, LoanRequest, LoanApproval, EMISchedule, LoanRepayment,
    LoanStatus, RepaymentStatus
)
from app.services.loan_service import (
    create_loan_request, cast_vote, get_loan_details, LoanError
)
from app.services.authorization_service import (
    can_vote, can_repay, is_group_member, is_group_admin, AuthorizationError
)
from app.services.wallet_service import submit_repayment, WalletError

loans_bp = Blueprint('loans', __name__)


# ============== CREATE LOAN REQUEST ==============
@loans_bp.route('/groups/<int:group_id>/loans/create', methods=['GET', 'POST'])
@login_required
def create_loan(group_id):
    group = Group.query.get_or_404(group_id)

    if not is_group_member(current_user.id, group_id):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    if request.method == 'POST':
        try:
            amount = float(request.form.get('amount', 0))
            reason = request.form.get('reason', '')

            loan = create_loan_request(
                group_id=group_id,
                user_id=current_user.id,
                amount=amount,
                reason=reason
            )

            flash('Loan request submitted successfully!', 'success')
            return redirect(url_for('loans.view_loan', loan_id=loan.id))

        except LoanError as e:
            flash(str(e), 'danger')
        except AuthorizationError as e:
            flash(str(e), 'danger')
        except ValueError:
            flash('Please enter a valid amount!', 'danger')

    return render_template('loans/create.html', group=group)


# ============== VIEW LOAN DETAILS ==============
@loans_bp.route('/loans/<int:loan_id>')
@login_required
def view_loan(loan_id):
    loan = LoanRequest.query.get_or_404(loan_id)
    group = loan.group

    if not is_group_member(current_user.id, loan.group_id):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    details = get_loan_details(loan_id)

    can_vote_result, vote_reason = can_vote(current_user.id, loan_id)

    user_vote = LoanApproval.query.filter_by(
        loan_id=loan_id,
        user_id=current_user.id
    ).first()

    all_votes = LoanApproval.query.filter_by(loan_id=loan_id).all()

    can_repay_result, _ = can_repay(current_user.id, loan_id)

    is_admin = is_group_admin(current_user.id, loan.group_id)

    # NEW: Can this admin perform final approval?
    can_final_approve = (
        is_admin and
        loan.status == LoanStatus.PRE_APPROVED.value and
        loan.requested_by != current_user.id
    )

    pending_repayments = []
    if is_admin:
        pending_repayments = LoanRepayment.query.filter_by(
            loan_id=loan_id,
            status=RepaymentStatus.PENDING.value
        ).all()

    return render_template(
        'loans/detail.html',
        loan=loan,
        group=group,
        details=details,
        can_vote=can_vote_result,
        vote_reason=vote_reason,
        user_vote=user_vote,
        all_votes=all_votes,
        can_repay=can_repay_result,
        is_admin=is_admin,
        can_final_approve=can_final_approve,  # ← NEW
        pending_repayments=pending_repayments
    )


# ============== FINAL ADMIN APPROVAL ==============
@loans_bp.route('/loans/<int:loan_id>/final-approve', methods=['POST'])
@login_required
def final_approve_loan(loan_id):
    loan = LoanRequest.query.get_or_404(loan_id)

    if not is_group_admin(current_user.id, loan.group_id):
        flash('Only group admin can perform final approval!', 'danger')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

    if loan.status != LoanStatus.PRE_APPROVED.value:
        flash('This loan is not in pre-approved state.', 'danger')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

    if loan.requested_by == current_user.id:
        flash('You cannot final-approve your own loan request.', 'danger')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

    loan.status = LoanStatus.APPROVED.value
    loan.approved_at = datetime.utcnow()
    db.session.commit()

    flash('Loan has been finally approved!', 'success')
    return redirect(url_for('loans.view_loan', loan_id=loan_id))



# ============== LIST LOANS IN GROUP ==============
@loans_bp.route('/groups/<int:group_id>/loans')
@login_required
def list_loans(group_id):
    group = Group.query.get_or_404(group_id)

    if not is_group_member(current_user.id, group_id):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    # Filter by status if provided
    status_filter = request.args.get('status', None)

    query = LoanRequest.query.filter_by(group_id=group_id, is_active=True)

    if status_filter:
        query = query.filter_by(status=status_filter)

    loans = query.order_by(LoanRequest.created_at.desc()).all()

    return render_template(
        'loans/list.html',
        group=group,
        loans=loans,
        status_filter=status_filter,
        LoanStatus=LoanStatus
    )


# ============== VOTE ON LOAN ==============
@loans_bp.route('/loans/<int:loan_id>/vote', methods=['POST'])
@login_required
def vote_loan(loan_id):
    try:
        vote_value = request.form.get('vote')
        comment = request.form.get('comment', '')

        if vote_value not in ['approve', 'reject']:
            flash('Invalid vote!', 'danger')
            return redirect(url_for('loans.view_loan', loan_id=loan_id))

        approved = (vote_value == 'approve')

        vote, new_status = cast_vote(
            loan_id=loan_id,
            user_id=current_user.id,
            approved=approved,
            comment=comment
        )

        if approved:
            flash('You approved this loan request!', 'success')
        else:
            flash('You rejected this loan request!', 'info')

        # Notify if status changed
        if new_status == LoanStatus.APPROVED.value:
            flash('Loan has been APPROVED by majority!', 'success')
        elif new_status == LoanStatus.REJECTED.value:
            flash('Loan has been REJECTED by majority.', 'warning')

    except LoanError as e:
        flash(str(e), 'danger')
    except AuthorizationError as e:
        flash(str(e), 'danger')

    return redirect(url_for('loans.view_loan', loan_id=loan_id))


# ============== MY LOANS ==============
@loans_bp.route('/my-loans')
@login_required
def my_loans():
    # Get all loans requested by current user
    my_requests = LoanRequest.query.filter_by(
        requested_by=current_user.id,
        is_active=True
    ).order_by(LoanRequest.created_at.desc()).all()

    # Get pending votes
    pending_votes = []
    memberships = current_user.get_active_memberships().all()

    for membership in memberships:
        group_loans = LoanRequest.query.filter_by(
            group_id=membership.group_id,
            status=LoanStatus.PENDING.value,
            is_active=True
        ).all()

        for loan in group_loans:
            if loan.requested_by == current_user.id:
                continue

            existing_vote = LoanApproval.query.filter_by(
                loan_id=loan.id,
                user_id=current_user.id
            ).first()

            if not existing_vote:
                pending_votes.append(loan)

    # Get my pending repayments
    my_pending_repayments = LoanRepayment.query.filter_by(
        paid_by=current_user.id,
        status=RepaymentStatus.PENDING.value
    ).all()

    return render_template(
        'loans/my_loans.html',
        my_requests=my_requests,
        pending_votes=pending_votes,
        my_pending_repayments=my_pending_repayments
    )


# ============== SUBMIT REPAYMENT ==============
@loans_bp.route('/loans/<int:loan_id>/repay', methods=['GET', 'POST'])
@login_required
def repay_loan(loan_id):
    loan = LoanRequest.query.get_or_404(loan_id)
    group = loan.group

    # Check authorization
    allowed, reason = can_repay(current_user.id, loan_id)
    if not allowed:
        flash(reason, 'danger')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

    # Get EMI schedule if applicable
    emi_schedule = []
    next_emi = None
    if loan.repayment_type == 'emi':
        emi_schedule = EMISchedule.query.filter_by(loan_id=loan_id).order_by(
            EMISchedule.installment_number
        ).all()
        # Find next unpaid EMI
        next_emi = EMISchedule.query.filter_by(
            loan_id=loan_id,
            is_paid=False
        ).order_by(EMISchedule.installment_number).first()

    remaining_amount = loan.get_remaining_amount()

    if request.method == 'POST':
        try:
            amount = float(request.form.get('amount', 0))
            description = request.form.get('description', '')
            emi_id = request.form.get('emi_id', type=int)

            repayment = submit_repayment(
                loan_id=loan_id,
                user_id=current_user.id,
                amount=amount,
                description=description,
                emi_schedule_id=emi_id
            )

            flash(
                f'Repayment of ₹{amount:.2f} submitted! Awaiting admin approval.',
                'success'
            )
            return redirect(url_for('loans.view_loan', loan_id=loan_id))

        except WalletError as e:
            flash(str(e), 'danger')
        except AuthorizationError as e:
            flash(str(e), 'danger')
        except ValueError:
            flash('Please enter a valid amount!', 'danger')

    return render_template(
        'loans/repay.html',
        loan=loan,
        group=group,
        remaining_amount=remaining_amount,
        emi_schedule=emi_schedule,
        next_emi=next_emi
    )

# ============== VIEW EMI SCHEDULE ==============
@loans_bp.route('/loans/<int:loan_id>/emi-schedule')
@login_required
def view_emi_schedule(loan_id):
    loan = LoanRequest.query.get_or_404(loan_id)

    if not is_group_member(current_user.id, loan.group_id):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    # Fetch all EMI records ordered by installment
    emi_schedule = EMISchedule.query.filter_by(loan_id=loan_id).order_by(
        EMISchedule.installment_number
    ).all()

    if not emi_schedule:
        flash('No EMI schedule found for this loan.', 'info')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

    # Calculate accurate totals from EMI records
    total_emi_sum = sum(e.emi_amount for e in emi_schedule)
    total_principal_sum = sum(e.principal_component for e in emi_schedule)
    total_interest_sum = sum(e.interest_component for e in emi_schedule)

    # Additional stats
    paid_installments = sum(1 for e in emi_schedule if e.is_paid)
    total_installments = len(emi_schedule)
    total_paid_amount = sum(e.paid_amount or e.emi_amount for e in emi_schedule if e.is_paid)

    return render_template(
        'loans/emi_schedule.html',
        loan=loan,
        emi_schedule=emi_schedule,
        total_emi_sum=total_emi_sum,
        total_principal_sum=total_principal_sum,
        total_interest_sum=total_interest_sum,
        paid_installments=paid_installments,
        total_installments=total_installments,
        total_paid_amount=total_paid_amount
    )


# ============== VIEW REPAYMENT HISTORY ==============
@loans_bp.route('/loans/<int:loan_id>/repayments')
@login_required
def repayment_history(loan_id):
    loan = LoanRequest.query.get_or_404(loan_id)

    if not is_group_member(current_user.id, loan.group_id):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    repayments = LoanRepayment.query.filter_by(loan_id=loan_id).order_by(
        LoanRepayment.submitted_at.desc()
    ).all()

    return render_template(
        'loans/repayment_history.html',
        loan=loan,
        repayments=repayments
    )