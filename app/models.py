from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from app.extensions import db


# ============== USER MODEL ==============
class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    groups_created = db.relationship('Group', backref='creator', lazy='dynamic')
    memberships = db.relationship('GroupMember', backref='user', lazy='dynamic')
    loan_requests = db.relationship('LoanRequest', backref='requester', lazy='dynamic')
    approvals = db.relationship('LoanApproval', backref='approver', lazy='dynamic')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.name}>'


# ============== GROUP MODEL ==============
class Group(db.Model):
    __tablename__ = 'groups'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(500))
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    members = db.relationship('GroupMember', backref='group', lazy='dynamic')
    loan_requests = db.relationship('LoanRequest', backref='group', lazy='dynamic')

    def get_member_count(self):
        return self.members.count()

    def is_member(self, user):
        return self.members.filter_by(user_id=user.id).first() is not None

    def __repr__(self):
        return f'<Group {self.name}>'


# ============== GROUP MEMBER MODEL ==============
class GroupMember(db.Model):
    __tablename__ = 'group_members'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('groups.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    role = db.Column(db.String(20), default='member')  # 'admin' or 'member'
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Prevent duplicate memberships
    __table_args__ = (
        db.UniqueConstraint('group_id', 'user_id', name='unique_group_member'),
    )

    def __repr__(self):
        return f'<GroupMember user={self.user_id} group={self.group_id}>'


# ============== LOAN REQUEST MODEL ==============
class LoanRequest(db.Model):
    __tablename__ = 'loan_requests'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('groups.id'), nullable=False)
    requested_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    reason = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(20), default='pending')  # pending, approved, rejected
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    approvals = db.relationship('LoanApproval', backref='loan_request', lazy='dynamic')

    def get_approval_count(self):
        """Count how many approved"""
        return self.approvals.filter_by(approved=True).count()

    def get_rejection_count(self):
        """Count how many rejected"""
        return self.approvals.filter_by(approved=False).count()

    def get_total_votes(self):
        """Total votes cast"""
        return self.approvals.count()

    def check_and_update_status(self):
        """Update status based on majority vote"""
        group = Group.query.get(self.group_id)
        total_members = group.get_member_count()

        # Exclude the requester from voting count
        eligible_voters = total_members - 1

        if eligible_voters <= 0:
            return

        approvals = self.get_approval_count()
        rejections = self.get_rejection_count()

        # Majority rule (more than 50%)
        required_approvals = (eligible_voters // 2) + 1

        if approvals >= required_approvals:
            self.status = 'approved'
        elif rejections >= required_approvals:
            self.status = 'rejected'

    def __repr__(self):
        return f'<LoanRequest ₹{self.amount} by user={self.requested_by}>'


# ============== LOAN APPROVAL MODEL ==============
class LoanApproval(db.Model):
    __tablename__ = 'loan_approvals'

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('loan_requests.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    approved = db.Column(db.Boolean, nullable=False)  # True = approve, False = reject
    comment = db.Column(db.String(200))  # Optional comment
    voted_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Prevent duplicate votes
    __table_args__ = (
        db.UniqueConstraint('loan_id', 'user_id', name='unique_loan_vote'),
    )

    def __repr__(self):
        status = "approved" if self.approved else "rejected"
        return f'<LoanApproval user={self.user_id} {status}>'