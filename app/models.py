"""
BACHAT GAT v2.0 - DATABASE MODELS
==================================

CRITICAL DESIGN PRINCIPLES:
1. WalletTransaction is the SINGLE SOURCE OF TRUTH
2. All balances are CACHED values, recalculated from ledger
3. Loan states follow STRICT state machine
4. All financial operations are ATOMIC
5. Soft deletes for audit safety
6. Idempotency keys prevent duplicate transactions
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from app.extensions import db
import uuid


# ============================================================
# ENUMS FOR TYPE SAFETY
# ============================================================

class LoanStatus(Enum):
    """
    Loan State Machine:
    PENDING → PRE_APPROVED → APPROVED → DISBURSED → COMPLETED
            ↘ REJECTED
    """
    PENDING = 'pending'
    PRE_APPROVED = 'pre_approved'
    APPROVED = 'approved'
    REJECTED = 'rejected'
    DISBURSED = 'disbursed'
    COMPLETED = 'completed'

class RepaymentType(Enum):
    """Loan repayment structure"""
    EMI = 'emi'
    BULLET = 'bullet'

class TransactionType(Enum):
    """Types of wallet transactions"""
    CONTRIBUTION = 'contribution'
    LOAN_DISBURSEMENT = 'loan_disbursement'
    REPAYMENT = 'repayment'
    INTEREST_DISTRIBUTION = 'interest_distribution'
    REFUND = 'refund'

class RepaymentStatus(Enum):
    """Repayment approval states"""
    PENDING = 'pending'
    APPROVED = 'approved'
    REJECTED = 'rejected'

class MemberRole(Enum):
    """Group member roles"""
    ADMIN = 'admin'
    MEMBER = 'member'


# ============================================================
# USER MODEL
# ============================================================
class User(UserMixin, db.Model):
    """Core user entity."""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    phone = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    groups_created = db.relationship('Group', backref='creator', lazy='dynamic')
    memberships = db.relationship('GroupMember', backref='user', lazy='dynamic',
                                  foreign_keys='GroupMember.user_id')
    loan_requests = db.relationship('LoanRequest', backref='requester', lazy='dynamic',
                                    foreign_keys='LoanRequest.requested_by')
    approvals = db.relationship('LoanApproval', backref='approver', lazy='dynamic')
    contributions = db.relationship('MemberContribution', backref='contributor', lazy='dynamic')
    repayments = db.relationship('LoanRepayment', backref='payer', lazy='dynamic',
                                 foreign_keys='LoanRepayment.paid_by')
    member_ledgers = db.relationship('MemberLedger', backref='member', lazy='dynamic')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def get_active_memberships(self):
        """Get only active group memberships"""
        return self.memberships.filter_by(is_active=True)

    def __repr__(self):
        return f'<User {self.id}: {self.name}>'


# ============================================================
# GROUP MODEL (UPDATED WITH NEW FIELDS)
# ============================================================
class Group(db.Model):
    """
    Savings group (Bachat Gat).
    Each group has ONE wallet and multiple members.
    """
    __tablename__ = 'groups'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(500))
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    # Interest configuration (set by admin)
    default_interest_rate = db.Column(db.Float, default=12.0)  # Annual %
    default_loan_duration_months = db.Column(db.Integer, default=12)
    default_repayment_type = db.Column(db.String(20), default=RepaymentType.EMI.value)
    use_flat_rate = db.Column(db.Boolean, default=False, nullable=False)  # NEW: Flat rate option

    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    members = db.relationship('GroupMember', backref='group', lazy='dynamic',
                              cascade='all, delete-orphan')
    loan_requests = db.relationship('LoanRequest', backref='group', lazy='dynamic',
                                    cascade='all, delete-orphan')
    wallet = db.relationship('GroupWallet', backref='group', uselist=False,
                             cascade='all, delete-orphan')

    def get_active_member_count(self):
        """Return count of active members only"""
        return self.members.filter_by(is_active=True).count()

    def get_member_count(self):
        """Alias for backward compatibility"""
        return self.get_active_member_count()

    def is_member(self, user):
        """Check if user is an active member"""
        return self.members.filter_by(user_id=user.id, is_active=True).first() is not None

    def is_admin(self, user):
        """Check if user is an active admin"""
        membership = self.members.filter_by(user_id=user.id, is_active=True).first()
        return membership and membership.role == MemberRole.ADMIN.value

    def get_admins(self):
        """Get all active admins"""
        return self.members.filter_by(role=MemberRole.ADMIN.value, is_active=True).all()

    def get_admin(self):
        """Get first active admin (for backward compatibility)"""
        return self.members.filter_by(role=MemberRole.ADMIN.value, is_active=True).first()

    def __repr__(self):
        return f'<Group {self.id}: {self.name}>'


# ============================================================
# GROUP MEMBER MODEL (FULL SOFT DELETE)
# ============================================================
class GroupMember(db.Model):
    """
    Membership record with soft delete support.

    SOFT DELETE: Members are never hard-deleted.
    Set is_active=False and deleted_at when member leaves.
    """
    __tablename__ = 'group_members'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('groups.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    role = db.Column(db.String(20), default=MemberRole.MEMBER.value)

    # Soft delete fields
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    deleted_at = db.Column(db.DateTime, nullable=True)
    deleted_reason = db.Column(db.String(255), nullable=True)

    joined_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Prevent duplicate active memberships
    __table_args__ = (
        db.UniqueConstraint('group_id', 'user_id', 'is_active',
                            name='unique_active_group_member'),
    )

    def soft_delete(self, reason=None):
        """Soft delete this membership"""
        self.is_active = False
        self.deleted_at = datetime.utcnow()
        self.deleted_reason = reason

    def __repr__(self):
        status = "active" if self.is_active else "inactive"
        return f'<GroupMember user={self.user_id} group={self.group_id} {status}>'


# ============================================================
# GROUP WALLET MODEL (CACHE MANAGEMENT)
# ============================================================
class GroupWallet(db.Model):
    """
    Financial wallet for a group.

    CRITICAL:
    - 'balance' is a CACHED value only
    - True balance is calculated from WalletTransaction ledger
    - Use recalculate_balance() to sync
    - 'is_dirty' flag indicates cache may be stale
    """
    __tablename__ = 'group_wallets'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('groups.id'), unique=True, nullable=False)

    # CACHED financial values (recalculated from ledger)
    balance = db.Column(db.Float, default=0.0, nullable=False)
    total_contributed = db.Column(db.Float, default=0.0, nullable=False)
    total_disbursed = db.Column(db.Float, default=0.0, nullable=False)
    total_interest_earned = db.Column(db.Float, default=0.0, nullable=False)

    # Cache management
    is_dirty = db.Column(db.Boolean, default=False, nullable=False)
    last_recalculated_at = db.Column(db.DateTime, default=datetime.utcnow)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    contributions = db.relationship('MemberContribution', backref='wallet', lazy='dynamic')
    transactions = db.relationship('WalletTransaction', backref='wallet', lazy='dynamic')
    member_ledgers = db.relationship('MemberLedger', backref='wallet', lazy='dynamic')

    def mark_dirty(self):
        """Mark cache as potentially stale"""
        self.is_dirty = True

    def mark_clean(self):
        """Mark cache as up-to-date"""
        self.is_dirty = False
        self.last_recalculated_at = datetime.utcnow()

    def __repr__(self):
        dirty = " [DIRTY]" if self.is_dirty else ""
        return f'<GroupWallet group={self.group_id} balance={self.balance}{dirty}>'


# ============================================================
# MEMBER LEDGER MODEL
# ============================================================
class MemberLedger(db.Model):
    """
    Personal contribution and earnings ledger for each member.

    Tracks:
    - Principal contributed
    - Interest earned from loans
    - Current balance (principal + interest)

    Used for profit distribution when loans are repaid.
    """
    __tablename__ = 'member_ledgers'

    id = db.Column(db.Integer, primary_key=True)
    wallet_id = db.Column(db.Integer, db.ForeignKey('group_wallets.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    # Financial tracking
    principal_contributed = db.Column(db.Float, default=0.0, nullable=False)
    interest_earned = db.Column(db.Float, default=0.0, nullable=False)
    total_balance = db.Column(db.Float, default=0.0, nullable=False)  # principal + interest

    # Tracking
    last_contribution_at = db.Column(db.DateTime, nullable=True)
    last_interest_credit_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Unique constraint
    __table_args__ = (
        db.UniqueConstraint('wallet_id', 'user_id', name='unique_member_ledger'),
    )

    def update_balance(self):
        """Recalculate total balance"""
        self.total_balance = self.principal_contributed + self.interest_earned

    def __repr__(self):
        return f'<MemberLedger user={self.user_id} balance={self.total_balance}>'

# ============================================================
# MEMBER CONTRIBUTION MODEL
# ============================================================
class MemberContribution(db.Model):
    """
    Individual contribution record.
    Each contribution creates a WalletTransaction AND updates MemberLedger.
    """
    __tablename__ = 'member_contributions'

    id = db.Column(db.Integer, primary_key=True)
    wallet_id = db.Column(db.Integer, db.ForeignKey('group_wallets.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.String(255), nullable=True)

    # Link to transaction
    transaction_id = db.Column(db.Integer, db.ForeignKey('wallet_transactions.id'), nullable=True)

    contributed_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<MemberContribution user={self.user_id} amount={self.amount}>'


# ============================================================
# WALLET TRANSACTION MODEL (LEDGER - UPDATED)
# ============================================================
# ============================================================
# WALLET TRANSACTION MODEL (LEDGER - FIXED)
# ============================================================
class WalletTransaction(db.Model):
    """
    CRITICAL: This is the SINGLE SOURCE OF TRUTH for all wallet movements.
    """
    __tablename__ = 'wallet_transactions'

    id = db.Column(db.Integer, primary_key=True)
    wallet_id = db.Column(db.Integer, db.ForeignKey('group_wallets.id'), nullable=False)

    # Transaction type
    transaction_type = db.Column(db.String(30), nullable=False)

    # Amount: positive = inflow, negative = outflow
    amount = db.Column(db.Float, nullable=False)

    # Running balance after this transaction (for audit)
    balance_after = db.Column(db.Float, nullable=True)

    # Reference to related entity
    reference_type = db.Column(db.String(50), nullable=True)
    reference_id = db.Column(db.Integer, nullable=True)

    # Who initiated this transaction
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    # For interest distribution: who received it
    beneficiary_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    # Description
    description = db.Column(db.String(255), nullable=True)

    # IDEMPOTENCY KEY
    idempotency_key = db.Column(db.String(64), unique=True, nullable=False)

    # Audit fields
    is_reversed = db.Column(db.Boolean, default=False)
    reversed_at = db.Column(db.DateTime, nullable=True)
    reversed_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # ✅ ADD THESE RELATIONSHIPS
    created_by_user = db.relationship('User', foreign_keys=[created_by], backref='transactions_created')
    beneficiary = db.relationship('User', foreign_keys=[beneficiary_id], backref='interest_received')
    reversed_by_user = db.relationship('User', foreign_keys=[reversed_by_id])

    @staticmethod
    def generate_idempotency_key():
        """Generate a unique idempotency key"""
        import uuid
        return str(uuid.uuid4())

    def __repr__(self):
        return f'<WalletTransaction {self.transaction_type} amount={self.amount}>'

# ============================================================
# LOAN REQUEST MODEL (MAJOR UPDATE)
# ============================================================
class LoanRequest(db.Model):
    """
    Loan request with explicit state machine.

    STATE MACHINE:
    PENDING → APPROVED → DISBURSED → COMPLETED
           ↘ REJECTED

    VOTING INTEGRITY:
    - total_eligible_voters is FROZEN at creation
    - Member changes don't affect ongoing votes
    """
    __tablename__ = 'loan_requests'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('groups.id'), nullable=False)
    requested_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    # Request details
    amount = db.Column(db.Float, nullable=False)
    reason = db.Column(db.String(500), nullable=False)

    # STATE MACHINE - Use LoanStatus enum
    status = db.Column(db.String(20), default=LoanStatus.PENDING.value, nullable=False)

    # VOTING INTEGRITY - Frozen at creation
    total_eligible_voters = db.Column(db.Integer, nullable=False)
    required_approvals = db.Column(db.Integer, nullable=False)

    # Interest & Repayment Configuration (set at approval)
    interest_rate = db.Column(db.Float, nullable=True)  # Annual %
    loan_duration_months = db.Column(db.Integer, nullable=True)
    repayment_type = db.Column(db.String(20), nullable=True)  # 'emi' or 'bullet'

    # Calculated values (set at approval)
    approved_amount = db.Column(db.Float, nullable=True)
    total_interest = db.Column(db.Float, nullable=True)
    total_repayable = db.Column(db.Float, nullable=True)  # principal + interest
    emi_amount = db.Column(db.Float, nullable=True)

    # Status timestamps
    approved_at = db.Column(db.DateTime, nullable=True)
    approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    rejected_at = db.Column(db.DateTime, nullable=True)
    rejected_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    disbursed_at = db.Column(db.DateTime, nullable=True)
    disbursed_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    # Repayment tracking
    total_principal_repaid = db.Column(db.Float, default=0.0, nullable=False)
    total_interest_repaid = db.Column(db.Float, default=0.0, nullable=False)
    total_repaid = db.Column(db.Float, default=0.0, nullable=False)

    # Soft delete
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    deleted_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    approvals = db.relationship('LoanApproval', backref='loan_request', lazy='dynamic')
    repayments = db.relationship('LoanRepayment', backref='loan', lazy='dynamic')
    emi_schedule = db.relationship('EMISchedule', backref='loan', lazy='dynamic',
                                   order_by='EMISchedule.installment_number')
    interest_distributions = db.relationship('InterestDistribution', backref='loan', lazy='dynamic')

    # Valid state transitions
    VALID_TRANSITIONS = {
        LoanStatus.PENDING.value: [LoanStatus.APPROVED.value, LoanStatus.REJECTED.value],
        LoanStatus.APPROVED.value: [LoanStatus.DISBURSED.value, LoanStatus.REJECTED.value],
        LoanStatus.REJECTED.value: [],  # Terminal state
        LoanStatus.DISBURSED.value: [LoanStatus.COMPLETED.value],
        LoanStatus.COMPLETED.value: [],  # Terminal state
    }

    def can_transition_to(self, new_status):
        """Check if transition to new_status is valid"""
        allowed = self.VALID_TRANSITIONS.get(self.status, [])
        return new_status in allowed

    def transition_to(self, new_status, by_user_id=None):
        """
        Transition to new state with validation.
        Raises ValueError if transition is invalid.
        """
        if not self.can_transition_to(new_status):
            raise ValueError(
                f"Invalid state transition: {self.status} → {new_status}"
            )

        old_status = self.status
        self.status = new_status

        # Set timestamps based on new status
        now = datetime.utcnow()
        if new_status == LoanStatus.APPROVED.value:
            self.approved_at = now
            self.approved_by = by_user_id
        elif new_status == LoanStatus.REJECTED.value:
            self.rejected_at = now
            self.rejected_by = by_user_id
        elif new_status == LoanStatus.DISBURSED.value:
            self.disbursed_at = now
            self.disbursed_by = by_user_id
        elif new_status == LoanStatus.COMPLETED.value:
            self.completed_at = now

        return old_status

    def get_approval_count(self):
        return self.approvals.filter_by(approved=True).count()

    def get_rejection_count(self):
        return self.approvals.filter_by(approved=False).count()

    def get_remaining_amount(self):
        """Calculate remaining amount to be repaid"""
        if not self.total_repayable:
            return 0.0
        return self.total_repayable - self.total_repaid

    def is_fully_repaid(self):
        """Check if loan is fully repaid"""
        if not self.total_repayable:
            return False
        return self.total_repaid >= self.total_repayable

    def soft_delete(self):
        self.is_active = False
        self.deleted_at = datetime.utcnow()

    def __repr__(self):
        return f'<LoanRequest {self.id} ₹{self.amount} status={self.status}>'


# ============================================================
# LOAN APPROVAL MODEL
# ============================================================
class LoanApproval(db.Model):
    """
    Vote record for loan request.
    Uses FROZEN total_eligible_voters from LoanRequest.
    """
    __tablename__ = 'loan_approvals'

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('loan_requests.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    approved = db.Column(db.Boolean, nullable=False)
    comment = db.Column(db.String(200))
    voted_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('loan_id', 'user_id', name='unique_loan_vote'),
    )

    def __repr__(self):
        vote = "approved" if self.approved else "rejected"
        return f'<LoanApproval user={self.user_id} {vote}>'


# ============================================================
# EMI SCHEDULE MODEL (NEW!)
# ============================================================
class EMISchedule(db.Model):
    """
    Pre-calculated EMI schedule for loans.
    Generated when loan is approved with EMI repayment type.
    """
    __tablename__ = 'emi_schedules'

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('loan_requests.id'), nullable=False)

    installment_number = db.Column(db.Integer, nullable=False)
    due_date = db.Column(db.Date, nullable=False)

    # Breakdown
    emi_amount = db.Column(db.Float, nullable=False)
    principal_component = db.Column(db.Float, nullable=False)
    interest_component = db.Column(db.Float, nullable=False)

    # Balance tracking
    opening_balance = db.Column(db.Float, nullable=False)
    closing_balance = db.Column(db.Float, nullable=False)

    # Payment status
    is_paid = db.Column(db.Boolean, default=False)
    paid_at = db.Column(db.DateTime, nullable=True)
    paid_amount = db.Column(db.Float, nullable=True)
    repayment_id = db.Column(db.Integer, db.ForeignKey('loan_repayments.id'), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('loan_id', 'installment_number', name='unique_loan_installment'),
    )

    def __repr__(self):
        status = "PAID" if self.is_paid else "DUE"
        return f'<EMI #{self.installment_number} ₹{self.emi_amount} {status}>'


# ============================================================
# LOAN CONTRIBUTION SNAPSHOT (NEW!)
# ============================================================
class LoanContributionSnapshot(db.Model):
    """
    Snapshot of member contributions at loan approval time.

    CRITICAL: This freezes contribution ratios for interest distribution.
    - Taken when loan is APPROVED
    - Excludes the borrower
    - Used to calculate interest distribution throughout loan lifecycle
    """
    __tablename__ = 'loan_contribution_snapshots'

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('loan_requests.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    # Contribution at time of loan approval
    contribution_amount = db.Column(db.Float, nullable=False)

    # Percentage of total eligible pool (excluding borrower)
    contribution_percentage = db.Column(db.Float, nullable=False)

    # Total eligible pool at snapshot time
    total_eligible_pool = db.Column(db.Float, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref='contribution_snapshots')
    loan = db.relationship('LoanRequest', backref='contribution_snapshots')

    # Unique constraint
    __table_args__ = (
        db.UniqueConstraint('loan_id', 'user_id', name='unique_loan_contribution_snapshot'),
    )

    def __repr__(self):
        return f'<LoanContributionSnapshot loan={self.loan_id} user={self.user_id} {self.contribution_percentage:.1f}%>'
# ============================================================
# LOAN REPAYMENT MODEL (UPDATED)
# ============================================================
class LoanRepayment(db.Model):
    """
    Loan repayment with approval workflow.

    WORKFLOW:
    1. Borrower submits repayment → status = PENDING
    2. Admin reviews
    3. Admin approves/rejects
    4. Only on APPROVAL: Wallet updated, interest distributed
    """
    __tablename__ = 'loan_repayments'

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('loan_requests.id'), nullable=False)
    paid_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    # Payment details
    amount = db.Column(db.Float, nullable=False)
    principal_component = db.Column(db.Float, nullable=True)
    interest_component = db.Column(db.Float, nullable=True)

    # For EMI: which installment is this for?
    emi_schedule_id = db.Column(db.Integer, db.ForeignKey('emi_schedules.id'), nullable=True)

    # APPROVAL WORKFLOW
    status = db.Column(db.String(20), default=RepaymentStatus.PENDING.value, nullable=False)

    # Approval details
    approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    rejection_reason = db.Column(db.String(255), nullable=True)

    # Transaction reference (created only after approval)
    transaction_id = db.Column(db.Integer, db.ForeignKey('wallet_transactions.id'), nullable=True)

    # Idempotency
    idempotency_key = db.Column(db.String(64), unique=True, nullable=False)

    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship to approver
    approver = db.relationship('User', foreign_keys=[approved_by])

    def approve(self, admin_user_id):
        """Approve this repayment"""
        if self.status != RepaymentStatus.PENDING.value:
            raise ValueError(f"Cannot approve repayment in {self.status} status")

        self.status = RepaymentStatus.APPROVED.value
        self.approved_by = admin_user_id
        self.approved_at = datetime.utcnow()

    def reject(self, admin_user_id, reason=None):
        """Reject this repayment"""
        if self.status != RepaymentStatus.PENDING.value:
            raise ValueError(f"Cannot reject repayment in {self.status} status")

        self.status = RepaymentStatus.REJECTED.value
        self.approved_by = admin_user_id
        self.approved_at = datetime.utcnow()
        self.rejection_reason = reason

    def __repr__(self):
        return f'<LoanRepayment ₹{self.amount} status={self.status}>'


# ============================================================
# INTEREST DISTRIBUTION MODEL (NEW!)
# ============================================================
class InterestDistribution(db.Model):
    """
    Tracks interest distribution to lenders when repayment is approved.

    When interest is repaid, it's distributed proportionally to all
    members who contributed to the wallet.
    """
    __tablename__ = 'interest_distributions'

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('loan_requests.id'), nullable=False)
    repayment_id = db.Column(db.Integer, db.ForeignKey('loan_repayments.id'), nullable=False)

    # Who receives the interest
    beneficiary_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    # Distribution details
    contribution_amount = db.Column(db.Float, nullable=False)  # Their contribution at time
    contribution_percentage = db.Column(db.Float, nullable=False)  # % of total pool
    interest_earned = db.Column(db.Float, nullable=False)  # Amount received

    # Transaction reference
    transaction_id = db.Column(db.Integer, db.ForeignKey('wallet_transactions.id'), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship
    beneficiary = db.relationship('User', foreign_keys=[beneficiary_id])

    def __repr__(self):
        return f'<InterestDistribution user={self.beneficiary_id} earned=₹{self.interest_earned}>'


# ============================================================
# ADMIN TRANSFER HISTORY MODEL (NEW!)
# ============================================================
class AdminTransferHistory(db.Model):
    """
    Audit trail for admin role transfers.

    Admin cannot leave group - must transfer first.
    System ensures at least one admin exists.
    """
    __tablename__ = 'admin_transfer_history'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('groups.id'), nullable=False)

    from_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    to_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    reason = db.Column(db.String(255), nullable=True)
    transferred_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    from_user = db.relationship('User', foreign_keys=[from_user_id])
    to_user = db.relationship('User', foreign_keys=[to_user_id])

    def __repr__(self):
        return f'<AdminTransfer group={self.group_id} from={self.from_user_id} to={self.to_user_id}>'