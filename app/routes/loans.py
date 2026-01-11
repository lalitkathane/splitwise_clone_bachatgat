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
    LoanStatus, RepaymentStatus, WalletTransaction
)
from app.services.loan_service import (
    create_loan_request, cast_vote, get_loan_details, LoanError,
    approve_loan_with_interest
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
    """Create a new loan request in a group"""
    group = Group.query.get_or_404(group_id)

    if not is_group_member(current_user.id, group_id):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    if request.method == 'POST':
        try:
            amount = float(request.form.get('amount', 0))
            reason = request.form.get('reason', '').strip()

            # Validate positive whole number
            if amount <= 0:
                flash('Please enter a valid amount greater than zero!', 'danger')
                return render_template('loans/create.html', group=group)

            if amount != int(amount):
                flash('Please enter a whole number (no decimals allowed)!', 'danger')
                return render_template('loans/create.html', group=group)

            amount = int(amount)  # Convert to integer
            if not reason:
                flash('Please provide a reason for the loan request!', 'danger')
                return render_template('loans/create.html', group=group)

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
    """View detailed information about a loan"""
    # Force a fresh query from database
    loan = LoanRequest.query.get_or_404(loan_id)
    group = loan.group

    if not is_group_member(current_user.id, loan.group_id):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    # IMPORTANT: Refresh the loan object to get latest data
    db.session.refresh(loan)

    details = get_loan_details(loan_id)
    can_vote_result, vote_reason = can_vote(current_user.id, loan_id)

    user_vote = LoanApproval.query.filter_by(
        loan_id=loan_id,
        user_id=current_user.id
    ).first()

    all_votes = LoanApproval.query.filter_by(loan_id=loan_id).all()
    can_repay_result, _ = can_repay(current_user.id, loan_id)
    is_admin = is_group_admin(current_user.id, loan.group_id)

    # Check if admin can perform final approval
    can_final_approve = (
            is_admin and
            loan.status == LoanStatus.PRE_APPROVED.value and
            loan.requested_by != current_user.id
    )

    # Get pending repayments for admin
    pending_repayments = []
    if is_admin:
        pending_repayments = LoanRepayment.query.filter_by(
            loan_id=loan_id,
            status=RepaymentStatus.PENDING.value
        ).all()

    today = datetime.utcnow().date()

    # Calculate vote progress percentage
    vote_progress = 0
    if loan.status == LoanStatus.PENDING.value and details.get('voting'):
        voting_dict = details.get('voting', {})
        required_approvals = voting_dict.get('required_approvals', 1)
        approvals = voting_dict.get('approvals', 0)
        vote_progress = (approvals / required_approvals * 100) if required_approvals > 0 else 0

    # Calculate repayment progress percentage
    repay_progress = 0
    if loan.total_repayable and loan.total_repayable > 0:
        repay_progress = (loan.total_repaid / loan.total_repayable * 100) if loan.total_repaid else 0
        repay_progress = min(100, repay_progress)  # Cap at 100%

    # Calculate days until next EMI (if applicable)
    days_left = 0
    if loan.status == LoanStatus.DISBURSED.value and details.get('next_emi'):
        # FIXED: Handle both dictionary and object access
        next_emi = details['next_emi']
        if isinstance(next_emi, dict):
            due_date = next_emi.get('due_date')
        else:
            # If it's an EMISchedule object
            due_date = getattr(next_emi, 'due_date', None)

        if due_date:
            days_left = (due_date - today).days
            days_left = max(0, days_left)

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
        can_final_approve=can_final_approve,
        pending_repayments=pending_repayments,
        today=today,
        vote_progress=vote_progress,
        repay_progress=repay_progress,
        days_left=days_left,
        now=datetime.utcnow()
    )

# ============== FINAL ADMIN APPROVAL ==============
@loans_bp.route('/loans/<int:loan_id>/final-approve', methods=['POST'])
@login_required
def final_approve_loan(loan_id):
    """Admin final approval for pre-approved loans"""
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
    """List all loans in a group with optional status filter"""
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
    """Cast vote on a loan request"""
    try:
        vote_value = request.form.get('vote')
        comment = request.form.get('comment', '').strip()

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


# ============== MY LOANS DASHBOARD ==============
@loans_bp.route('/my-loans')
@login_required
def my_loans():
    """Personal dashboard showing user's loans and pending actions"""
    # Get all loans requested by current user
    my_requests = LoanRequest.query.filter_by(
        requested_by=current_user.id,
        is_active=True
    ).order_by(LoanRequest.created_at.desc()).all()

    # Get pending votes (loans in user's groups that need voting)
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

    # Get my pending repayments (repayments I submitted awaiting admin approval)
    my_pending_repayments = LoanRepayment.query.filter_by(
        paid_by=current_user.id,
        status=RepaymentStatus.PENDING.value
    ).all()

    return render_template(
        'loans/my_loans.html',
        my_requests=my_requests,
        pending_votes=pending_votes,
        my_pending_repayments=my_pending_repayments,
        today=datetime.utcnow()
    )


# ============== SUBMIT REPAYMENT ==============
# In loans.py, update the repay_loan function to hide when fully repaid:

@loans_bp.route('/loans/<int:loan_id>/repay', methods=['GET', 'POST'])
@login_required
def repay_loan(loan_id):
    """Submit a repayment for a loan"""
    loan = LoanRequest.query.get_or_404(loan_id)

    # Check if loan is fully repaid
    if loan.is_fully_repaid():
        flash('This loan has already been fully repaid!', 'info')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

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
            amount_str = request.form.get('amount', '0').strip()

            # Validate it's a number
            try:
                amount = float(amount_str)
            except ValueError:
                flash('Please enter a valid amount!', 'danger')
                return render_template('loans/repay.html',
                                       loan=loan, group=group, remaining_amount=remaining_amount,
                                       emi_schedule=emi_schedule, next_emi=next_emi)

            # Validate positive amount
            if amount <= 0:
                flash('Please enter a valid amount greater than zero!', 'danger')
                return render_template('loans/repay.html',
                                       loan=loan, group=group, remaining_amount=remaining_amount,
                                       emi_schedule=emi_schedule, next_emi=next_emi)

            # Check if amount is a whole number
            if not amount.is_integer():
                flash('Repayment amount must be a whole number (no decimals allowed)!', 'danger')
                return render_template('loans/repay.html',
                                       loan=loan, group=group, remaining_amount=remaining_amount,
                                       emi_schedule=emi_schedule, next_emi=next_emi)

            amount = int(amount)

            description = request.form.get('description', '').strip()
            emi_id = request.form.get('emi_id', type=int)

            repayment = submit_repayment(
                loan_id=loan_id,
                user_id=current_user.id,
                amount=amount,
                description=description,
                emi_schedule_id=emi_id
            )

            flash(
                f'Repayment of ₹{amount:,} submitted! Awaiting admin approval.',
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
    """View detailed EMI schedule for a loan"""
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
    """View repayment history for a loan"""
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


# ============== CLOSE LOAN (ADMIN ONLY) ==============
@loans_bp.route('/loans/<int:loan_id>/close', methods=['POST'])
@login_required
def close_loan(loan_id):
    """Admin: Close a fully repaid loan"""
    loan = LoanRequest.query.get_or_404(loan_id)

    if not is_group_admin(current_user.id, loan.group_id):
        flash('Only group admin can close loans!', 'danger')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

    if loan.status != LoanStatus.DISBURSED.value:
        flash('Loan must be disbursed to close.', 'danger')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

    if not loan.is_fully_repaid():
        flash('Loan must be fully repaid to close.', 'danger')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

    try:
        loan.transition_to(LoanStatus.COMPLETED.value, current_user.id)
        db.session.commit()
        flash('Loan has been closed successfully!', 'success')
    except ValueError as e:
        flash(str(e), 'danger')

    return redirect(url_for('loans.view_loan', loan_id=loan_id))


# ============== EDIT LOAN (ADMIN ONLY) ==============
# In loans.py, update the edit_loan function:

# ============== EDIT LOAN (ADMIN ONLY) ==============
@loans_bp.route('/loans/<int:loan_id>/edit', methods=['POST'])
@login_required
def edit_loan(loan_id):
    """Admin: Edit loan terms with validation and EMI regeneration"""
    loan = LoanRequest.query.get_or_404(loan_id)

    if not is_group_admin(current_user.id, loan.group_id):
        flash('Only group admin can edit loans!', 'danger')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

    try:
        change_reason = request.form.get('change_reason', '').strip()
        if not change_reason:
            flash('Please provide a reason for the changes.', 'danger')
            return redirect(url_for('loans.view_loan', loan_id=loan_id))

        # Get form data
        amount = request.form.get('amount')
        interest_rate = request.form.get('interest_rate')
        loan_duration = request.form.get('loan_duration')
        repayment_type = request.form.get('repayment_type')
        remarks = request.form.get('remarks', '').strip()
        notes = request.form.get('notes', '').strip()

        # Track what changed for audit
        changes = []
        financial_terms_changed = False

        # Check current state before changes
        old_amount = loan.amount
        old_interest_rate = loan.interest_rate
        old_duration = loan.loan_duration_months
        old_repayment_type = loan.repayment_type
        old_total_repayable = loan.total_repayable
        old_emi_amount = loan.emi_amount

        # ============== VALIDATE AND UPDATE FIELDS ==============

        # Update loan amount (only if pending or pre-approved)
        if loan.status in [LoanStatus.PENDING.value, LoanStatus.PRE_APPROVED.value] and amount:
            try:
                new_amount = float(amount)
                if new_amount <= 0:
                    flash('Amount must be greater than 0', 'danger')
                    return redirect(url_for('loans.view_loan', loan_id=loan_id))

                # Check if amount is a whole number
                if new_amount != int(new_amount):
                    flash('Amount must be a whole number (no decimals)', 'danger')
                    return redirect(url_for('loans.view_loan', loan_id=loan_id))

                new_amount = int(new_amount)

                if new_amount != loan.amount:
                    changes.append(f"Amount: ₹{loan.amount} → ₹{new_amount}")
                    loan.amount = new_amount
                    if loan.status in [LoanStatus.PRE_APPROVED.value, LoanStatus.APPROVED.value]:
                        loan.approved_amount = new_amount
                    financial_terms_changed = True
            except ValueError:
                flash('Invalid amount format', 'danger')
                return redirect(url_for('loans.view_loan', loan_id=loan_id))

        # Update interest rate
        if interest_rate:
            try:
                new_rate = float(interest_rate)
                if new_rate < 0:
                    flash('Interest rate cannot be negative', 'danger')
                    return redirect(url_for('loans.view_loan', loan_id=loan_id))

                if loan.interest_rate is None or new_rate != loan.interest_rate:
                    changes.append(f"Interest rate: {loan.interest_rate or 'N/A'}% → {new_rate}%")
                    loan.interest_rate = new_rate
                    financial_terms_changed = True
            except ValueError:
                flash('Invalid interest rate format', 'danger')
                return redirect(url_for('loans.view_loan', loan_id=loan_id))

        # Update loan duration
        if loan_duration:
            try:
                new_duration = int(loan_duration)
                if new_duration <= 0:
                    flash('Loan duration must be greater than 0', 'danger')
                    return redirect(url_for('loans.view_loan', loan_id=loan_id))

                if loan.loan_duration_months is None or new_duration != loan.loan_duration_months:
                    changes.append(f"Duration: {loan.loan_duration_months or 'N/A'} months → {new_duration} months")
                    loan.loan_duration_months = new_duration
                    financial_terms_changed = True
            except ValueError:
                flash('Invalid loan duration format', 'danger')
                return redirect(url_for('loans.view_loan', loan_id=loan_id))

        # Update repayment type
        if repayment_type and repayment_type in ['emi', 'bullet']:
            if loan.repayment_type is None or repayment_type != loan.repayment_type:
                changes.append(f"Repayment type: {loan.repayment_type or 'N/A'} → {repayment_type}")
                loan.repayment_type = repayment_type
                financial_terms_changed = True

        # ============== REGENERATE EMI SCHEDULE IF NEEDED ==============
        if financial_terms_changed and loan.status in [
            LoanStatus.PRE_APPROVED.value,
            LoanStatus.APPROVED.value,
            LoanStatus.DISBURSED.value
        ]:
            # Check if there are any paid EMIs
            paid_emis_count = EMISchedule.query.filter_by(
                loan_id=loan.id,
                is_paid=True
            ).count()

            if paid_emis_count > 0:
                flash(
                    'Cannot regenerate EMI schedule - some installments have already been paid. Please contact support.',
                    'danger')
                db.session.rollback()
                return redirect(url_for('loans.view_loan', loan_id=loan_id))

            # Delete all existing EMIs
            EMISchedule.query.filter_by(loan_id=loan.id).delete()
            db.session.flush()

            # Reset loan financials
            loan.total_interest = 0
            loan.total_repayable = 0
            loan.emi_amount = None
            loan.total_repaid = 0
            loan.total_principal_repaid = 0
            loan.total_interest_repaid = 0

            # Recalculate with new terms using the loan service
            approve_loan_with_interest(loan, is_regeneration=True)

            # COMMIT THE CHANGES HERE
            db.session.commit()

            changes.append("EMI schedule regenerated with new terms")

            # Log the regeneration
            print(f"✅ Loan #{loan.id} EMI schedule regenerated after term changes")
            print(f"   Old: Amount={old_amount}, Rate={old_interest_rate}%, Duration={old_duration} months")
            print(
                f"   New: Amount={loan.amount}, Rate={loan.interest_rate}%, Duration={loan.loan_duration_months} months")
            print(f"   New EMI: ₹{loan.emi_amount}, Total Repayable: ₹{loan.total_repayable}")

        # ============== UPDATE NOTES/REMARKS ==============
        if remarks:
            timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            new_remark = f"[{timestamp}] ADMIN: {remarks} (Reason: {change_reason})"
            if loan.admin_remarks:
                loan.admin_remarks += '\n' + new_remark
            else:
                loan.admin_remarks = new_remark
            changes.append("Added admin remarks")

        if notes:
            timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            new_note = f"[{timestamp}] USER: {notes}"
            if loan.borrower_notes:
                loan.borrower_notes += '\n' + new_note
            else:
                loan.borrower_notes = new_note
            changes.append("Added borrower notes")

        # ============== UPDATE LOAN STATUS IF NEEDED ==============
        # If loan was pre-approved and terms changed, it might need re-approval
        if financial_terms_changed and loan.status == LoanStatus.PRE_APPROVED.value:
            # Check if approval conditions are still met
            approval_count = loan.get_approval_count()
            required_approvals = loan.required_approvals

            if approval_count >= required_approvals:
                # Still has majority approval, keep as pre-approved
                changes.append("Loan maintains majority approval")
            else:
                # No longer has majority, revert to pending
                loan.status = LoanStatus.PENDING.value
                changes.append("Loan reverted to pending (lost majority approval)")

        loan.last_updated_at = datetime.utcnow()
        loan.last_updated_by = current_user.id

        # Commit all changes if not already committed during EMI regeneration
        if not financial_terms_changed or loan.status not in [
            LoanStatus.PRE_APPROVED.value,
            LoanStatus.APPROVED.value,
            LoanStatus.DISBURSED.value
        ]:
            db.session.commit()

        # Refresh the loan object to get updated values
        db.session.refresh(loan)

        if changes:
            flash(f'Loan updated successfully! Changes: {", ".join(changes)}', 'success')

            # Log detailed changes
            print(f"📝 Loan #{loan.id} updated by Admin {current_user.id}:")
            for change in changes:
                print(f"   - {change}")
        else:
            flash('No changes were made.', 'info')

    except Exception as e:
        db.session.rollback()
        flash(f'Error updating loan: {str(e)}', 'danger')
        import traceback
        print(f"❌ Error in edit_loan: {str(e)}")
        traceback.print_exc()

    # Redirect with cache busting parameter
    import time
    return redirect(url_for('loans.view_loan', loan_id=loan_id, _t=int(time.time())))


# ============== LOAN AUDIT LOGS (ADMIN ONLY) ==============
@loans_bp.route('/loans/<int:loan_id>/audit-logs')
@login_required
def loan_audit_logs(loan_id):
    """View comprehensive audit logs for a specific loan"""
    loan = LoanRequest.query.get_or_404(loan_id)

    if not is_group_admin(current_user.id, loan.group_id):
        flash('Only group admin can view audit logs!', 'danger')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

    # Gather audit data from related models
    approvals = LoanApproval.query.filter_by(loan_id=loan_id).order_by(
        LoanApproval.voted_at.desc()
    ).all()

    repayments = LoanRepayment.query.filter_by(loan_id=loan_id).order_by(
        LoanRepayment.submitted_at.desc()
    ).all()

    # Get wallet transactions related to this loan
    transactions = []
    if loan.group and loan.group.wallet:
        transactions = WalletTransaction.query.filter(
            WalletTransaction.wallet_id == loan.group.wallet.id,
            WalletTransaction.reference_type == 'loan',
            WalletTransaction.reference_id == loan_id
        ).order_by(WalletTransaction.created_at.desc()).all()

    return render_template(
        'loans/audit_logs.html',
        loan=loan,
        approvals=approvals,
        repayments=repayments,
        transactions=transactions
    )