from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.extensions import db
from app.models import Group, GroupMember, LoanRequest, LoanApproval

loans_bp = Blueprint('loans', __name__)


# ============== CREATE LOAN REQUEST ==============
@loans_bp.route('/groups/<int:group_id>/loans/create', methods=['GET', 'POST'])
@login_required
def create_loan(group_id):
    group = Group.query.get_or_404(group_id)

    # Check if user is member
    if not group.is_member(current_user):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    if request.method == 'POST':
        amount = request.form.get('amount')
        reason = request.form.get('reason')

        # Validation
        if not amount or not reason:
            flash('Amount and reason are required!', 'danger')
            return redirect(url_for('loans.create_loan', group_id=group_id))

        try:
            amount = float(amount)
            if amount <= 0:
                raise ValueError
        except ValueError:
            flash('Please enter a valid amount!', 'danger')
            return redirect(url_for('loans.create_loan', group_id=group_id))

        # Check if user already has a pending loan in this group
        existing_loan = LoanRequest.query.filter_by(
            group_id=group_id,
            requested_by=current_user.id,
            status='pending'
        ).first()

        if existing_loan:
            flash('You already have a pending loan request in this group!', 'warning')
            return redirect(url_for('loans.view_loan', loan_id=existing_loan.id))

        # Create loan request
        new_loan = LoanRequest(
            group_id=group_id,
            requested_by=current_user.id,
            amount=amount,
            reason=reason,
            status='pending'
        )
        db.session.add(new_loan)
        db.session.commit()

        flash('Loan request submitted successfully!', 'success')
        return redirect(url_for('loans.view_loan', loan_id=new_loan.id))

    return render_template('loans/create.html', group=group)


# ============== VIEW SINGLE LOAN REQUEST ==============
@loans_bp.route('/loans/<int:loan_id>')
@login_required
def view_loan(loan_id):
    loan = LoanRequest.query.get_or_404(loan_id)
    group = loan.group

    # Check if user is member of the group
    if not group.is_member(current_user):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    # Get all approvals/rejections
    approvals = LoanApproval.query.filter_by(loan_id=loan_id).all()

    # Check if current user has voted
    user_vote = LoanApproval.query.filter_by(
        loan_id=loan_id,
        user_id=current_user.id
    ).first()

    # Calculate voting stats
    total_members = group.get_member_count()
    eligible_voters = total_members - 1  # Exclude requester
    votes_cast = loan.get_total_votes()
    votes_needed = (eligible_voters // 2) + 1  # Majority

    # Can current user vote?
    can_vote = (
            loan.status == 'pending' and
            current_user.id != loan.requested_by and
            user_vote is None
    )

    return render_template(
        'loans/detail.html',
        loan=loan,
        group=group,
        approvals=approvals,
        user_vote=user_vote,
        can_vote=can_vote,
        total_members=total_members,
        eligible_voters=eligible_voters,
        votes_cast=votes_cast,
        votes_needed=votes_needed
    )


# ============== LIST ALL LOANS IN GROUP ==============
@loans_bp.route('/groups/<int:group_id>/loans')
@login_required
def list_loans(group_id):
    group = Group.query.get_or_404(group_id)

    # Check if user is member
    if not group.is_member(current_user):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    # Get all loans in the group
    loans = LoanRequest.query.filter_by(group_id=group_id).order_by(
        LoanRequest.created_at.desc()
    ).all()

    return render_template('loans/list.html', group=group, loans=loans)


# ============== VOTE ON LOAN (APPROVE/REJECT) ==============
@loans_bp.route('/loans/<int:loan_id>/vote', methods=['POST'])
@login_required
def vote_loan(loan_id):
    loan = LoanRequest.query.get_or_404(loan_id)
    group = loan.group

    # Check if user is member
    if not group.is_member(current_user):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    # Cannot vote on own loan
    if current_user.id == loan.requested_by:
        flash('You cannot vote on your own loan request!', 'danger')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

    # Check if loan is still pending
    if loan.status != 'pending':
        flash('This loan request is no longer pending!', 'warning')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

    # Check if already voted
    existing_vote = LoanApproval.query.filter_by(
        loan_id=loan_id,
        user_id=current_user.id
    ).first()

    if existing_vote:
        flash('You have already voted on this loan!', 'warning')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

    # Get vote from form
    vote = request.form.get('vote')
    comment = request.form.get('comment', '')

    if vote not in ['approve', 'reject']:
        flash('Invalid vote!', 'danger')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

    # Create vote
    new_vote = LoanApproval(
        loan_id=loan_id,
        user_id=current_user.id,
        approved=(vote == 'approve'),
        comment=comment
    )
    db.session.add(new_vote)
    db.session.commit()

    # Check and update loan status
    loan.check_and_update_status()
    db.session.commit()

    if vote == 'approve':
        flash('You approved this loan request!', 'success')
    else:
        flash('You rejected this loan request!', 'info')

    return redirect(url_for('loans.view_loan', loan_id=loan_id))


# ============== MY LOAN REQUESTS ==============
@loans_bp.route('/my-loans')
@login_required
def my_loans():
    # Get all loans requested by current user
    my_requests = LoanRequest.query.filter_by(
        requested_by=current_user.id
    ).order_by(LoanRequest.created_at.desc()).all()

    # Get all pending votes (loans where user needs to vote)
    pending_votes = []
    memberships = GroupMember.query.filter_by(user_id=current_user.id).all()

    for membership in memberships:
        group_loans = LoanRequest.query.filter_by(
            group_id=membership.group_id,
            status='pending'
        ).all()

        for loan in group_loans:
            # Skip own loans
            if loan.requested_by == current_user.id:
                continue

            # Check if already voted
            existing_vote = LoanApproval.query.filter_by(
                loan_id=loan.id,
                user_id=current_user.id
            ).first()

            if not existing_vote:
                pending_votes.append(loan)

    return render_template(
        'loans/my_loans.html',
        my_requests=my_requests,
        pending_votes=pending_votes
    )