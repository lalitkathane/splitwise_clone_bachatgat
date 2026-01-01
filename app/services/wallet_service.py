"""
WALLET SERVICE MODULE
=====================

This module contains all financial operations for the Bachat Gat system.

CRITICAL RULES:
1. Wallet balance MUST only change via WalletTransaction
2. All amounts must be validated (> 0)
3. Authorization checks are mandatory
4. Use database transactions to prevent race conditions

Functions:
- create_wallet_for_group()
- contribute_to_wallet()
- disburse_loan()
- repay_loan()
- recalculate_wallet_balance()
- get_wallet_summary()
"""

from datetime import datetime
from app.extensions import db
from app.models import (
    Group, GroupWallet, MemberContribution, WalletTransaction,
    LoanRequest, LoanRepayment, GroupMember
)


# ============================================================
# CUSTOM EXCEPTIONS
# ============================================================

class WalletError(Exception):
    """Base exception for wallet operations."""
    pass


class InsufficientBalanceError(WalletError):
    """Raised when wallet has insufficient balance."""
    pass


class UnauthorizedError(WalletError):
    """Raised when user is not authorized for an operation."""
    pass


class InvalidAmountError(WalletError):
    """Raised when amount is invalid (<=0 or None)."""
    pass


class LoanNotApprovedError(WalletError):
    """Raised when trying to disburse a non-approved loan."""
    pass


class LoanAlreadyDisbursedError(WalletError):
    """Raised when trying to disburse an already disbursed loan."""
    pass


class LoanFullyRepaidError(WalletError):
    """Raised when trying to repay an already fully repaid loan."""
    pass


class LoanNotDisbursedError(WalletError):
    """Raised when trying to repay a loan that hasn't been disbursed."""
    pass


# ============================================================
# WALLET CREATION
# ============================================================

def create_wallet_for_group(group_id):
    """
    Create a wallet for a group.
    Should be called automatically when a group is created.

    Args:
        group_id: The ID of the group

    Returns:
        GroupWallet: The created wallet

    Raises:
        ValueError: If group doesn't exist or already has a wallet
    """
    group = Group.query.get(group_id)
    if not group:
        raise ValueError(f"Group with ID {group_id} does not exist")

    if group.wallet:
        raise ValueError(f"Group {group_id} already has a wallet")

    wallet = GroupWallet(
        group_id=group_id,
        balance=0.0,
        total_contributed=0.0,
        total_disbursed=0.0
    )

    db.session.add(wallet)
    db.session.commit()

    return wallet


# ============================================================
# CONTRIBUTION
# ============================================================

def contribute_to_wallet(wallet_id, user_id, amount, description=None):
    """
    Add a contribution to the group wallet.

    This function:
    1. Validates the amount
    2. Checks user is a group member
    3. Creates a MemberContribution record
    4. Creates a WalletTransaction record
    5. Updates wallet balance and totals

    Args:
        wallet_id: The wallet ID
        user_id: The contributing user's ID
        amount: The contribution amount (must be > 0)
        description: Optional description

    Returns:
        tuple: (MemberContribution, WalletTransaction)

    Raises:
        InvalidAmountError: If amount <= 0
        UnauthorizedError: If user is not a group member
    """
    # Validate amount
    if not amount or amount <= 0:
        raise InvalidAmountError("Contribution amount must be greater than 0")

    # Get wallet and group
    wallet = GroupWallet.query.get(wallet_id)
    if not wallet:
        raise ValueError(f"Wallet with ID {wallet_id} does not exist")

    group = wallet.group

    # Check if user is a member of the group
    membership = GroupMember.query.filter_by(
        group_id=group.id,
        user_id=user_id
    ).first()

    if not membership:
        raise UnauthorizedError("User is not a member of this group")

    # Create contribution record
    contribution = MemberContribution(
        wallet_id=wallet_id,
        user_id=user_id,
        amount=amount,
        contributed_at=datetime.utcnow()
    )
    db.session.add(contribution)
    db.session.flush()  # Get the contribution ID

    # Create transaction record (positive amount = inflow)
    transaction = WalletTransaction(
        wallet_id=wallet_id,
        transaction_type='contribution',
        amount=amount,  # Positive for contribution
        reference_id=contribution.id,
        created_by=user_id,
        description=description or f"Contribution by user {user_id}",
        created_at=datetime.utcnow()
    )
    db.session.add(transaction)

    # Update wallet balance and totals
    wallet.balance += amount
    wallet.total_contributed += amount

    db.session.commit()

    return contribution, transaction


# ============================================================
# LOAN DISBURSEMENT
# ============================================================

def disburse_loan(loan_id, disbursed_by_user_id):
    """
    Disburse an approved loan from the group wallet.

    This function:
    1. Validates the loan is approved
    2. Checks loan hasn't already been disbursed
    3. Verifies sufficient wallet balance
    4. Verifies the user is a group admin
    5. Creates a WalletTransaction record (negative amount)
    6. Updates loan disbursement status
    7. Updates wallet balance and totals

    Args:
        loan_id: The loan request ID
        disbursed_by_user_id: The admin user performing disbursement

    Returns:
        WalletTransaction: The disbursement transaction

    Raises:
        LoanNotApprovedError: If loan status is not 'approved'
        LoanAlreadyDisbursedError: If loan was already disbursed
        InsufficientBalanceError: If wallet balance < approved_amount
        UnauthorizedError: If user is not a group admin
    """
    # Get loan
    loan = LoanRequest.query.get(loan_id)
    if not loan:
        raise ValueError(f"Loan with ID {loan_id} does not exist")

    # Check loan is approved
    if loan.status != 'approved':
        raise LoanNotApprovedError(
            f"Loan is not approved. Current status: {loan.status}"
        )

    # Check loan hasn't been disbursed
    if loan.disbursed_at:
        raise LoanAlreadyDisbursedError("This loan has already been disbursed")

    # Get group and wallet
    group = loan.group
    wallet = group.wallet

    if not wallet:
        raise ValueError("Group does not have a wallet")

    # Get disbursement amount
    disburse_amount = loan.approved_amount or loan.amount

    # Check sufficient balance
    if wallet.balance < disburse_amount:
        raise InsufficientBalanceError(
            f"Insufficient balance. Required: ₹{disburse_amount}, Available: ₹{wallet.balance}"
        )

    # Check user is admin of the group
    membership = GroupMember.query.filter_by(
        group_id=group.id,
        user_id=disbursed_by_user_id
    ).first()

    if not membership or membership.role != 'admin':
        raise UnauthorizedError("Only group admin can disburse loans")

    # Create transaction record (negative amount = outflow)
    transaction = WalletTransaction(
        wallet_id=wallet.id,
        transaction_type='loan_disbursement',
        amount=-disburse_amount,  # Negative for disbursement
        reference_id=loan.id,
        created_by=disbursed_by_user_id,
        description=f"Loan disbursement to user {loan.requested_by}",
        created_at=datetime.utcnow()
    )
    db.session.add(transaction)

    # Update loan status
    loan.disbursed_at = datetime.utcnow()
    if not loan.approved_amount:
        loan.approved_amount = loan.amount

    # Update wallet balance and totals
    wallet.balance -= disburse_amount
    wallet.total_disbursed += disburse_amount

    db.session.commit()

    return transaction


# ============================================================
# LOAN REPAYMENT
# ============================================================

def repay_loan(loan_id, user_id, amount, description=None):
    """
    Record a loan repayment.

    This function:
    1. Validates the amount
    2. Checks loan was disbursed
    3. Checks loan is not already fully repaid
    4. Verifies user is the borrower
    5. Limits repayment to remaining amount
    6. Creates a LoanRepayment record
    7. Creates a WalletTransaction record (positive amount)
    8. Updates loan repayment totals
    9. Marks loan as fully repaid if complete

    Args:
        loan_id: The loan request ID
        user_id: The user making the repayment (must be borrower)
        amount: The repayment amount (must be > 0)
        description: Optional description

    Returns:
        tuple: (LoanRepayment, WalletTransaction, is_fully_repaid)

    Raises:
        InvalidAmountError: If amount <= 0
        LoanNotDisbursedError: If loan hasn't been disbursed yet
        LoanFullyRepaidError: If loan is already fully repaid
        UnauthorizedError: If user is not the borrower
    """
    # Validate amount
    if not amount or amount <= 0:
        raise InvalidAmountError("Repayment amount must be greater than 0")

    # Get loan
    loan = LoanRequest.query.get(loan_id)
    if not loan:
        raise ValueError(f"Loan with ID {loan_id} does not exist")

    # Check loan was disbursed
    if not loan.disbursed_at:
        raise LoanNotDisbursedError("This loan has not been disbursed yet")

    # Check loan is not fully repaid
    if loan.is_fully_repaid:
        raise LoanFullyRepaidError("This loan has already been fully repaid")

    # Check user is the borrower
    if loan.requested_by != user_id:
        raise UnauthorizedError("Only the borrower can repay this loan")

    # Get group and wallet
    group = loan.group
    wallet = group.wallet

    if not wallet:
        raise ValueError("Group does not have a wallet")

    # Calculate remaining amount
    remaining = loan.get_remaining_amount()

    # Limit repayment to remaining amount
    actual_repayment = min(amount, remaining)

    # Create repayment record
    repayment = LoanRepayment(
        loan_id=loan_id,
        paid_by=user_id,
        amount=actual_repayment,
        paid_at=datetime.utcnow()
    )
    db.session.add(repayment)
    db.session.flush()  # Get the repayment ID

    # Create transaction record (positive amount = inflow)
    transaction = WalletTransaction(
        wallet_id=wallet.id,
        transaction_type='repayment',
        amount=actual_repayment,  # Positive for repayment
        reference_id=repayment.id,
        created_by=user_id,
        description=description or f"Loan repayment by user {user_id}",
        created_at=datetime.utcnow()
    )
    db.session.add(transaction)

    # Update loan totals
    loan.total_repaid += actual_repayment

    # Check if fully repaid
    is_fully_repaid = loan.total_repaid >= loan.approved_amount
    if is_fully_repaid:
        loan.is_fully_repaid = True

    # Update wallet balance
    wallet.balance += actual_repayment

    db.session.commit()

    return repayment, transaction, is_fully_repaid


# ============================================================
# BALANCE RECALCULATION
# ============================================================

def recalculate_wallet_balance(wallet_id):
    """
    Recalculate wallet balance from all transactions.

    This is a safety function to ensure balance accuracy.
    Should be used for auditing or if inconsistencies are suspected.

    Args:
        wallet_id: The wallet ID

    Returns:
        dict: {
            'previous_balance': float,
            'calculated_balance': float,
            'difference': float,
            'was_corrected': bool
        }
    """
    wallet = GroupWallet.query.get(wallet_id)
    if not wallet:
        raise ValueError(f"Wallet with ID {wallet_id} does not exist")

    # Get all transactions for this wallet
    transactions = WalletTransaction.query.filter_by(wallet_id=wallet_id).all()

    # Calculate balance from transactions
    calculated_balance = sum(t.amount for t in transactions)

    # Calculate totals
    contributions = sum(t.amount for t in transactions if t.transaction_type == 'contribution')
    disbursements = sum(abs(t.amount) for t in transactions if t.transaction_type == 'loan_disbursement')
    repayments = sum(t.amount for t in transactions if t.transaction_type == 'repayment')

    previous_balance = wallet.balance
    difference = calculated_balance - previous_balance

    # Correct if there's a difference
    was_corrected = False
    if abs(difference) > 0.01:  # Allow for floating point tolerance
        wallet.balance = calculated_balance
        wallet.total_contributed = contributions
        wallet.total_disbursed = disbursements
        was_corrected = True
        db.session.commit()

    return {
        'previous_balance': previous_balance,
        'calculated_balance': calculated_balance,
        'difference': difference,
        'was_corrected': was_corrected,
        'total_contributed': contributions,
        'total_disbursed': disbursements,
        'total_repaid': repayments
    }


# ============================================================
# WALLET SUMMARY
# ============================================================

def get_wallet_summary(wallet_id):
    """
    Get a comprehensive summary of wallet status.

    Args:
        wallet_id: The wallet ID

    Returns:
        dict: Wallet summary including balance, transactions, etc.
    """
    wallet = GroupWallet.query.get(wallet_id)
    if not wallet:
        raise ValueError(f"Wallet with ID {wallet_id} does not exist")

    # Get transaction counts
    contribution_count = wallet.transactions.filter_by(
        transaction_type='contribution'
    ).count()

    disbursement_count = wallet.transactions.filter_by(
        transaction_type='loan_disbursement'
    ).count()

    repayment_count = wallet.transactions.filter_by(
        transaction_type='repayment'
    ).count()

    # Get contribution by member
    member_contributions = db.session.query(
        MemberContribution.user_id,
        db.func.sum(MemberContribution.amount).label('total')
    ).filter_by(wallet_id=wallet_id).group_by(
        MemberContribution.user_id
    ).all()

    return {
        'wallet_id': wallet.id,
        'group_id': wallet.group_id,
        'group_name': wallet.group.name,
        'balance': wallet.balance,
        'total_contributed': wallet.total_contributed,
        'total_disbursed': wallet.total_disbursed,
        'total_repaid': wallet.get_total_repaid(),
        'transaction_counts': {
            'contributions': contribution_count,
            'disbursements': disbursement_count,
            'repayments': repayment_count,
            'total': contribution_count + disbursement_count + repayment_count
        },
        'member_contributions': [
            {'user_id': mc.user_id, 'total': mc.total}
            for mc in member_contributions
        ],
        'created_at': wallet.created_at
    }


# ============================================================
# MEMBER CONTRIBUTION SUMMARY
# ============================================================

def get_member_contribution_total(wallet_id, user_id):
    """
    Get total contribution by a specific member.

    Args:
        wallet_id: The wallet ID
        user_id: The user ID

    Returns:
        float: Total contribution amount
    """
    total = db.session.query(db.func.sum(MemberContribution.amount))\
        .filter_by(wallet_id=wallet_id, user_id=user_id).scalar()

    return total or 0.0


# ============================================================
# PENDING DISBURSEMENT CHECK
# ============================================================

def get_pending_disbursements(group_id):
    """
    Get all approved loans that haven't been disbursed yet.

    Args:
        group_id: The group ID

    Returns:
        list: List of LoanRequest objects pending disbursement
    """
    return LoanRequest.query.filter_by(
        group_id=group_id,
        status='approved',
        disbursed_at=None
    ).all()


# ============================================================
# ACTIVE LOANS
# ============================================================

def get_active_loans(group_id):
    """
    Get all loans that have been disbursed but not fully repaid.

    Args:
        group_id: The group ID

    Returns:
        list: List of active LoanRequest objects
    """
    return LoanRequest.query.filter(
        LoanRequest.group_id == group_id,
        LoanRequest.disbursed_at.isnot(None),
        LoanRequest.is_fully_repaid == False
    ).all()