"""
GROUP MANAGEMENT ROUTES
=======================
Clean, simplified group management.
"""

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.extensions import db
from app.models import Group, GroupMember, User, MemberRole, LoanRequest, LoanStatus
from app.services.wallet_service import create_wallet_for_group
from app.services.membership_service import (
    add_member, leave_group, remove_member, transfer_admin,
    get_member_liabilities, MembershipError
)
from app.services.authorization_service import (
    is_group_admin, is_group_member, AuthorizationError
)

groups_bp = Blueprint('groups', __name__)


# ============== LIST ALL MY GROUPS ==============
@groups_bp.route('/groups')
@login_required
def list_groups():
    memberships = current_user.get_active_memberships().all()
    my_groups = [m.group for m in memberships]
    return render_template('groups/list.html', groups=my_groups)


# ============== CREATE NEW GROUP ==============
@groups_bp.route('/groups/create', methods=['GET', 'POST'])
@login_required
def create_group():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()

        if not name:
            flash('Group name is required!', 'danger')
            return redirect(url_for('groups.create_group'))

        try:
            # Create group
            new_group = Group(
                name=name,
                description=request.form.get('description', '').strip(),
                created_by=current_user.id,
                default_interest_rate=request.form.get('interest_rate', 12.0, type=float),
                default_loan_duration_months=request.form.get('loan_duration', 12, type=int),
                default_repayment_type=request.form.get('repayment_type', 'emi'),
                use_flat_rate='use_flat_rate' in request.form
            )
            db.session.add(new_group)
            db.session.flush()

            # Add creator as admin
            db.session.add(GroupMember(
                group_id=new_group.id,
                user_id=current_user.id,
                role=MemberRole.ADMIN.value
            ))
            db.session.flush()

            # Create wallet
            create_wallet_for_group(new_group.id)
            db.session.commit()

            flash(f'Group "{name}" created successfully!', 'success')
            return redirect(url_for('groups.view_group', group_id=new_group.id))

        except Exception as e:
            db.session.rollback()
            flash(f'Error creating group: {str(e)}', 'danger')

    return render_template('groups/create.html')


# ============== VIEW SINGLE GROUP ==============
@groups_bp.route('/groups/<int:group_id>')
@login_required
def view_group(group_id):
    group = Group.query.get_or_404(group_id)

    if not is_group_member(current_user.id, group_id):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    members = GroupMember.query.filter_by(group_id=group_id, is_active=True).all()
    is_admin = is_group_admin(current_user.id, group_id)

    # Pending loans
    pending_loans = LoanRequest.query.filter_by(
        group_id=group_id,
        status=LoanStatus.PENDING.value,
        is_active=True
    ).order_by(LoanRequest.created_at.desc()).all()

    # Admin-only data
    awaiting_disbursement = []
    pending_repayments = []

    if is_admin:
        awaiting_disbursement = LoanRequest.query.filter_by(
            group_id=group_id,
            status=LoanStatus.APPROVED.value,
            is_active=True
        ).filter(LoanRequest.disbursed_at.is_(None)).all()

        from app.models import LoanRepayment, RepaymentStatus
        pending_repayments = LoanRepayment.query.join(LoanRequest).filter(
            LoanRequest.group_id == group_id,
            LoanRepayment.status == RepaymentStatus.PENDING.value
        ).all()

    return render_template(
        'groups/detail.html',
        group=group,
        members=members,
        is_admin=is_admin,
        wallet=group.wallet,
        pending_loans=pending_loans,
        awaiting_disbursement=awaiting_disbursement,
        pending_repayments=pending_repayments
    )


# ============== GROUP SETTINGS (Admin) ==============
@groups_bp.route('/groups/<int:group_id>/settings', methods=['GET', 'POST'])
@login_required
def group_settings(group_id):
    group = Group.query.get_or_404(group_id)

    if not is_group_admin(current_user.id, group_id):
        flash('Only admin can access settings!', 'danger')
        return redirect(url_for('groups.view_group', group_id=group_id))

    if request.method == 'POST':
        group.name = request.form.get('name', group.name).strip()
        group.description = request.form.get('description', group.description).strip()
        group.default_interest_rate = request.form.get('interest_rate', group.default_interest_rate, type=float)
        group.default_loan_duration_months = request.form.get('loan_duration', group.default_loan_duration_months, type=int)
        group.default_repayment_type = request.form.get('repayment_type', group.default_repayment_type)
        group.use_flat_rate = 'use_flat_rate' in request.form

        db.session.commit()
        flash('Settings updated!', 'success')
        return redirect(url_for('groups.view_group', group_id=group_id))

    return render_template('groups/settings.html', group=group)


# ============== ADD MEMBER ==============
@groups_bp.route('/groups/<int:group_id>/add-member', methods=['GET', 'POST'])
@login_required
def add_member_route(group_id):
    group = Group.query.get_or_404(group_id)

    if not is_group_admin(current_user.id, group_id):
        flash('Only admin can add members!', 'danger')
        return redirect(url_for('groups.view_group', group_id=group_id))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user = User.query.filter_by(email=email).first()

        if not user:
            flash('User not found with this email!', 'danger')
        else:
            try:
                add_member(group_id, user.id, current_user.id)
                flash(f'{user.name} added to group!', 'success')
                return redirect(url_for('groups.view_group', group_id=group_id))
            except (MembershipError, AuthorizationError) as e:
                flash(str(e), 'warning')

    return render_template('groups/add_member.html', group=group)


# ============== REMOVE MEMBER ==============
@groups_bp.route('/groups/<int:group_id>/remove-member/<int:user_id>', methods=['POST'])
@login_required
def remove_member_route(group_id, user_id):
    try:
        remove_member(group_id, user_id, current_user.id)
        flash('Member removed!', 'success')
    except (MembershipError, AuthorizationError) as e:
        flash(str(e), 'danger')

    return redirect(url_for('groups.view_group', group_id=group_id))


# ============== LEAVE GROUP ==============
@groups_bp.route('/groups/<int:group_id>/leave', methods=['GET', 'POST'])
@login_required
def leave_group_route(group_id):
    group = Group.query.get_or_404(group_id)
    liabilities = get_member_liabilities(current_user.id, group_id)

    if request.method == 'POST':
        if not liabilities['can_leave']:
            flash('Cannot leave - you have outstanding liabilities!', 'danger')
            return redirect(url_for('groups.leave_group_route', group_id=group_id))

        try:
            leave_group(group_id, current_user.id)
            flash('You left the group.', 'info')
            return redirect(url_for('groups.list_groups'))
        except (MembershipError, AuthorizationError) as e:
            flash(str(e), 'danger')
            return redirect(url_for('groups.view_group', group_id=group_id))

    return render_template('groups/leave.html', group=group, liabilities=liabilities)


# ============== TRANSFER ADMIN ==============
@groups_bp.route('/groups/<int:group_id>/transfer-admin', methods=['GET', 'POST'])
@login_required
def transfer_admin_route(group_id):
    group = Group.query.get_or_404(group_id)

    if not is_group_admin(current_user.id, group_id):
        flash('Only admin can transfer rights!', 'danger')
        return redirect(url_for('groups.view_group', group_id=group_id))

    eligible_members = GroupMember.query.filter(
        GroupMember.group_id == group_id,
        GroupMember.is_active == True,
        GroupMember.role != MemberRole.ADMIN.value,
        GroupMember.user_id != current_user.id
    ).all()

    if request.method == 'POST':
        to_user_id = request.form.get('to_user_id', type=int)
        reason = request.form.get('reason', '').strip()

        if not to_user_id:
            flash('Please select a member!', 'danger')
        else:
            try:
                transfer_admin(group_id, current_user.id, to_user_id, reason)
                flash('Admin rights transferred!', 'success')
                return redirect(url_for('groups.view_group', group_id=group_id))
            except (MembershipError, AuthorizationError) as e:
                flash(str(e), 'danger')

    return render_template('groups/transfer_admin.html', group=group, eligible_members=eligible_members)


# ============== VIEW MEMBER PROFILE ==============
@groups_bp.route('/groups/<int:group_id>/member/<int:user_id>')
@login_required
def view_member(group_id, user_id):
    group = Group.query.get_or_404(group_id)

    if not is_group_member(current_user.id, group_id):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    member = User.query.get_or_404(user_id)
    membership = GroupMember.query.filter_by(
        group_id=group_id, user_id=user_id, is_active=True
    ).first_or_404()

    from app.models import MemberLedger
    ledger = MemberLedger.query.filter_by(
        wallet_id=group.wallet.id, user_id=user_id
    ).first() if group.wallet else None

    loans = LoanRequest.query.filter_by(
        group_id=group_id, requested_by=user_id, is_active=True
    ).all()

    return render_template(
        'groups/member_profile.html',
        group=group,
        member=member,
        membership=membership,
        ledger=ledger,
        loans=loans
    )