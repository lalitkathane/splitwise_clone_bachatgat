from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.extensions import db
from app.models import Group, GroupMember, User

groups_bp = Blueprint('groups', __name__)


# ============== LIST ALL MY GROUPS ==============
@groups_bp.route('/groups')
@login_required
def list_groups():
    # Get all groups where current user is a member
    my_memberships = GroupMember.query.filter_by(user_id=current_user.id).all()
    my_groups = [membership.group for membership in my_memberships]

    return render_template('groups/list.html', groups=my_groups)


# ============== CREATE NEW GROUP ==============
@groups_bp.route('/groups/create', methods=['GET', 'POST'])
@login_required
def create_group():
    if request.method == 'POST':
        name = request.form.get('name')
        description = request.form.get('description')

        # Validation
        if not name:
            flash('Group name is required!', 'danger')
            return redirect(url_for('groups.create_group'))

        # Create group
        new_group = Group(
            name=name,
            description=description,
            created_by=current_user.id
        )
        db.session.add(new_group)
        db.session.commit()

        # Add creator as admin member
        membership = GroupMember(
            group_id=new_group.id,
            user_id=current_user.id,
            role='admin'
        )
        db.session.add(membership)
        db.session.commit()

        flash(f'Group "{name}" created successfully!', 'success')
        return redirect(url_for('groups.view_group', group_id=new_group.id))

    return render_template('groups/create.html')


# ============== VIEW SINGLE GROUP ==============
@groups_bp.route('/groups/<int:group_id>')
@login_required
def view_group(group_id):
    group = Group.query.get_or_404(group_id)

    # Check if user is member
    if not group.is_member(current_user):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    # Get all members
    members = GroupMember.query.filter_by(group_id=group_id).all()

    # Check if current user is admin
    current_membership = GroupMember.query.filter_by(
        group_id=group_id,
        user_id=current_user.id
    ).first()
    is_admin = current_membership.role == 'admin'

    return render_template(
        'groups/detail.html',
        group=group,
        members=members,
        is_admin=is_admin
    )


# ============== ADD MEMBER TO GROUP ==============
@groups_bp.route('/groups/<int:group_id>/add-member', methods=['GET', 'POST'])
@login_required
def add_member(group_id):
    group = Group.query.get_or_404(group_id)

    # Check if user is admin
    membership = GroupMember.query.filter_by(
        group_id=group_id,
        user_id=current_user.id
    ).first()

    if not membership or membership.role != 'admin':
        flash('Only admin can add members!', 'danger')
        return redirect(url_for('groups.view_group', group_id=group_id))

    if request.method == 'POST':
        email = request.form.get('email')

        # Find user by email
        user = User.query.filter_by(email=email).first()

        if not user:
            flash('User not found with this email!', 'danger')
            return redirect(url_for('groups.add_member', group_id=group_id))

        # Check if already a member
        existing = GroupMember.query.filter_by(
            group_id=group_id,
            user_id=user.id
        ).first()

        if existing:
            flash('User is already a member!', 'warning')
            return redirect(url_for('groups.add_member', group_id=group_id))

        # Add member
        new_member = GroupMember(
            group_id=group_id,
            user_id=user.id,
            role='member'
        )
        db.session.add(new_member)
        db.session.commit()

        flash(f'{user.name} added to group!', 'success')
        return redirect(url_for('groups.view_group', group_id=group_id))

    return render_template('groups/add_member.html', group=group)


# ============== REMOVE MEMBER FROM GROUP ==============
@groups_bp.route('/groups/<int:group_id>/remove-member/<int:user_id>', methods=['POST'])
@login_required
def remove_member(group_id, user_id):
    group = Group.query.get_or_404(group_id)

    # Check if current user is admin
    current_membership = GroupMember.query.filter_by(
        group_id=group_id,
        user_id=current_user.id
    ).first()

    if not current_membership or current_membership.role != 'admin':
        flash('Only admin can remove members!', 'danger')
        return redirect(url_for('groups.view_group', group_id=group_id))

    # Cannot remove yourself if you're the only admin
    if user_id == current_user.id:
        flash('You cannot remove yourself!', 'danger')
        return redirect(url_for('groups.view_group', group_id=group_id))

    # Find and remove member
    membership = GroupMember.query.filter_by(
        group_id=group_id,
        user_id=user_id
    ).first()

    if membership:
        db.session.delete(membership)
        db.session.commit()
        flash('Member removed!', 'success')

    return redirect(url_for('groups.view_group', group_id=group_id))


# ============== LEAVE GROUP ==============
@groups_bp.route('/groups/<int:group_id>/leave', methods=['POST'])
@login_required
def leave_group(group_id):
    membership = GroupMember.query.filter_by(
        group_id=group_id,
        user_id=current_user.id
    ).first()

    if not membership:
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    # Check if admin and only admin
    if membership.role == 'admin':
        admin_count = GroupMember.query.filter_by(
            group_id=group_id,
            role='admin'
        ).count()

        if admin_count == 1:
            flash('You are the only admin. Transfer admin role first!', 'danger')
            return redirect(url_for('groups.view_group', group_id=group_id))

    db.session.delete(membership)
    db.session.commit()
    flash('You left the group!', 'info')

    return redirect(url_for('groups.list_groups'))