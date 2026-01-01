from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from app.extensions import db


# ============================================================
# USER MODEL
# ============================================================
class User(UserMixin, db.Model):
    """
    Represents a registered user in the system.
    Users can create groups, join groups, request loans, and vote.
    """
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    groups_created = db.relationship('Group', backref='creator', lazy='dynamic')
    memberships = db.relationship('GroupMember', backref='user', lazy='dynamic')
    loan_requests = db.relationship('LoanRequest', backref='requester', lazy='dynamic',
                                    foreign_keys='LoanRequest.requested_by')
    approvals = db.relationship('LoanApproval', backref='approver', lazy='dynamic')
    contributions = db.relationship('MemberContribution', backref='contributor', lazy='dynamic')
    repayments = db.relationship('LoanRepayment', backref='payer', lazy='dynamic')
    transactions_created = db.relationship('WalletTransaction', backref='created_by_user', lazy='dynamic')

    def set_password(self, password):
        """Hash and set the user's password."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Verify password against stored hash."""
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.name}>'


# ============================================================
# GROUP MODEL
# ============================================================
class Group(db.Model):
    """
    Represents a savings group (Bachat Gat).
    Each group has members, a wallet, and can process loan requests.
    """
    __tablename__ = 'groups'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(500))
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    members = db.relationship('GroupMember', backref='group', lazy='dynamic',
                              cascade='all, delete-orphan')
    loan_requests = db.relationship('LoanRequest', backref='group', lazy='dynamic',
                                    cascade='all, delete-orphan')

    # One-to-One relationship with GroupWallet
    wallet = db.relationship('GroupWallet', backref='group', uselist=False,
                             cascade='all, delete-orphan')

    def get_member_count(self):
        """Return total number of members in the group."""
        return self.members.count()

    def is_member(self, user):
        """Check if a user is a member of this group."""
        return self.members.filter_by(user_id=user.id).first() is not None

    def is_admin(self, user):
        """Check if a user is an admin of this group."""
        membership = self.members.filter_by(user_id=user.id).first()
        return membership and membership.role == 'admin'

    def __repr__(self):
        return f'<Group {self.name}>'


# ============================================================
# GROUP MEMBER MODEL
# ============================================================
class GroupMember(db.Model):
    """
    Represents membership of a user in a group.
    Tracks role (admin/member) and join date.
    """
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


# ============================================================
# GROUP WALLET MODEL (NEW)
# ============================================================
class GroupWallet(db.Model):
    """
    Financial wallet for a group.

    CRITICAL: The 'balance' field should ONLY be modified through
    WalletTransaction entries to maintain financial integrity.

    One Group → One Wallet (1:1 relationship)
    """
    __tablename__ = 'group_wallets'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('groups.id'), unique=True, nullable=False)

    # Financial tracking fields
    balance = db.Column(db.Float, default=0.0, nullable=False)
    total_contributed = db.Column(db.Float, default=0.0, nullable=False)
    total_disbursed = db.Column(db.Float, default=0.0, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    contributions = db.relationship('MemberContribution', backref='wallet', lazy='dynamic',
                                    cascade='all, delete-orphan')
    transactions = db.relationship('WalletTransaction', backref='wallet', lazy='dynamic',
                                   cascade='all, delete-orphan')

    def get_total_repaid(self):
        """Calculate total repayments received."""
        total = db.session.query(db.func.sum(WalletTransaction.amount)) \
            .filter_by(wallet_id=self.id, transaction_type='repayment').scalar()
        return total or 0.0

    def __repr__(self):
        return f'<GroupWallet group={self.group_id} balance={self.balance}>'


# ============================================================
# MEMBER CONTRIBUTION MODEL (NEW)
# ============================================================
class MemberContribution(db.Model):
    """
    Tracks individual contributions made by members to the group wallet.
    Each contribution creates a corresponding WalletTransaction.
    """
    __tablename__ = 'member_contributions'

    id = db.Column(db.Integer, primary_key=True)
    wallet_id = db.Column(db.Integer, db.ForeignKey('group_wallets.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)  # Must be > 0
    contributed_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<MemberContribution user={self.user_id} amount={self.amount}>'


# ============================================================
# WALLET TRANSACTION MODEL (NEW - LEDGER)
# ============================================================
class WalletTransaction(db.Model):
    """
    CRITICAL: This is the single source of truth for all wallet movements.

    Every financial operation MUST create a transaction entry:
    - 'contribution': Money added to wallet (positive)
    - 'loan_disbursement': Money given as loan (negative)
    - 'repayment': Loan repayment received (positive)

    The wallet balance should be recalculated from transactions for accuracy.
    """
    __tablename__ = 'wallet_transactions'

    id = db.Column(db.Integer, primary_key=True)
    wallet_id = db.Column(db.Integer, db.ForeignKey('group_wallets.id'), nullable=False)

    # Transaction type: 'contribution', 'loan_disbursement', 'repayment'
    transaction_type = db.Column(db.String(30), nullable=False)

    # Amount: positive for inflow, negative for outflow
    amount = db.Column(db.Float, nullable=False)

    # Reference to related entity (contribution_id, loan_id, or repayment_id)
    reference_id = db.Column(db.Integer, nullable=True)

    # Who initiated this transaction
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    # Description/notes for the transaction
    description = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Valid transaction types
    VALID_TYPES = ['contribution', 'loan_disbursement', 'repayment']

    def __repr__(self):
        return f'<WalletTransaction {self.transaction_type} amount={self.amount}>'


# ============================================================
# LOAN REQUEST MODEL (UPDATED)
# ============================================================
class LoanRequest(db.Model):
    """
    Represents a loan request made by a group member.

    Lifecycle:
    1. Created with status='pending'
    2. Members vote (approve/reject)
    3. If approved, status='approved' and can be disbursed
    4. After disbursement, disbursed_at is set
    5. Borrower makes repayments
    6. When fully repaid, is_fully_repaid=True
    """
    __tablename__ = 'loan_requests'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('groups.id'), nullable=False)
    requested_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    # Original request amount
    amount = db.Column(db.Float, nullable=False)
    reason = db.Column(db.String(500), nullable=False)

    # Voting status: 'pending', 'approved', 'rejected'
    status = db.Column(db.String(20), default='pending')

    # ===== NEW FINANCIAL FIELDS =====

    # Amount actually approved (may differ from requested amount)
    approved_amount = db.Column(db.Float, nullable=True)

    # When the loan was disbursed from wallet
    disbursed_at = db.Column(db.DateTime, nullable=True)

    # Track repayment progress
    total_repaid = db.Column(db.Float, default=0.0, nullable=False)
    is_fully_repaid = db.Column(db.Boolean, default=False, nullable=False)

    # ===== END NEW FIELDS =====

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    approvals = db.relationship('LoanApproval', backref='loan_request', lazy='dynamic',
                                cascade='all, delete-orphan')
    repayments = db.relationship('LoanRepayment', backref='loan', lazy='dynamic',
                                 cascade='all, delete-orphan')

    def get_approval_count(self):
        """Count how many members approved."""
        return self.approvals.filter_by(approved=True).count()

    def get_rejection_count(self):
        """Count how many members rejected."""
        return self.approvals.filter_by(approved=False).count()

    def get_total_votes(self):
        """Total votes cast."""
        return self.approvals.count()

    def check_and_update_status(self):
        """
        Update loan status based on majority vote.
        Does NOT handle disbursement - that's a separate step.
        """
        group = Group.query.get(self.group_id)
        total_members = group.get_member_count()

        # Exclude the requester from voting
        eligible_voters = total_members - 1

        if eligible_voters <= 0:
            return

        approvals = self.get_approval_count()
        rejections = self.get_rejection_count()

        # Majority rule (more than 50%)
        required_approvals = (eligible_voters // 2) + 1

        if approvals >= required_approvals:
            self.status = 'approved'
            self.approved_amount = self.amount  # Default: approve full amount
        elif rejections >= required_approvals:
            self.status = 'rejected'

    def is_disbursed(self):
        """Check if loan has been disbursed."""
        return self.disbursed_at is not None

    def get_remaining_amount(self):
        """Calculate remaining amount to be repaid."""
        if not self.approved_amount:
            return 0.0
        return self.approved_amount - self.total_repaid

    def __repr__(self):
        return f'<LoanRequest ₹{self.amount} by user={self.requested_by} status={self.status}>'


# ============================================================
# LOAN APPROVAL MODEL
# ============================================================
class LoanApproval(db.Model):
    """
    Records a member's vote on a loan request.
    Each member can vote only once per loan.
    """
    __tablename__ = 'loan_approvals'

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('loan_requests.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    approved = db.Column(db.Boolean, nullable=False)  # True = approve, False = reject
    comment = db.Column(db.String(200))
    voted_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Prevent duplicate votes
    __table_args__ = (
        db.UniqueConstraint('loan_id', 'user_id', name='unique_loan_vote'),
    )

    def __repr__(self):
        status = "approved" if self.approved else "rejected"
        return f'<LoanApproval user={self.user_id} {status}>'


# ============================================================
# LOAN REPAYMENT MODEL (NEW)
# ============================================================
class LoanRepayment(db.Model):
    """
    Tracks individual repayment installments for a loan.
    Each repayment creates a corresponding WalletTransaction.

    Only the borrower can make repayments on their own loan.
    """
    __tablename__ = 'loan_repayments'

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('loan_requests.id'), nullable=False)
    paid_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)  # Must be > 0
    paid_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<LoanRepayment loan={self.loan_id} amount={self.amount}>'