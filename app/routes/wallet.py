"""
WALLET ROUTES
=============

Uses wallet_service for all financial operations.
All operations are atomic.
"""

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.extensions import db
from app.models import (
    Group, GroupWallet, WalletTransaction, MemberLedger, LoanRequest,
    LoanRepayment, LoanStatus, RepaymentStatus, TransactionType
)
from app.services.wallet_service import (
    contribute_to_wallet, disburse_loan, approve_repayment,
    recalculate_wallet_balance, get_wallet_summary,
    WalletError, InsufficientBalanceError, InvalidAmountError,
    DuplicateTransactionError
)
from app.services.authorization_service import (
    can_contribute, can_disburse, can_approve_repayment,
    is_group_member, is_group_admin, AuthorizationError
)

wallet_bp = Blueprint('wallet', __name__)


# ============== VIEW WALLET ==============
@wallet_bp.route('/groups/<int:group_id>/wallet')
@login_required
def view_wallet(group_id):
    group = Group.query.get_or_404(group_id)
    if not is_group_member(current_user.id, group_id):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    wallet = group.wallet
    if not wallet:
        flash('This group does not have a wallet!', 'danger')
        return redirect(url_for('groups.view_group', group_id=group_id))

    # Get wallet summary
    try:
        summary = get_wallet_summary(wallet.id)
    except Exception as e:
        flash(f'Error loading wallet: {str(e)}', 'danger')
        summary = None

    # Get pending disbursements (for admin)
    pending_disbursements = []
    if is_group_admin(current_user.id, group_id):
        pending_disbursements = LoanRequest.query.filter_by(
            group_id=group_id,
            status=LoanStatus.APPROVED.value,
            is_active=True
        ).filter(LoanRequest.disbursed_at.is_(None)).all()

    # Get active loans
    active_loans = LoanRequest.query.filter(
        LoanRequest.group_id == group_id,
        LoanRequest.status == LoanStatus.DISBURSED.value,
        LoanRequest.is_active == True
    ).all()

    # Get recent transactions
    recent_transactions = WalletTransaction.query.filter_by(
        wallet_id=wallet.id,
        is_reversed=False
    ).order_by(WalletTransaction.created_at.desc()).limit(10).all()

    # Get recent ledgers (for admin)
    recent_ledgers = []
    if is_group_admin(current_user.id, group_id):
        recent_ledgers = MemberLedger.query.filter_by(
            wallet_id=wallet.id
        ).order_by(MemberLedger.updated_at.desc(), MemberLedger.created_at.desc()).limit(10).all()

    is_admin = is_group_admin(current_user.id, group_id)

    return render_template(
        'wallet/view.html',
        group=group,
        wallet=wallet,
        summary=summary,
        recent_ledgers=recent_ledgers,
        pending_disbursements=pending_disbursements,
        active_loans=active_loans,
        recent_transactions=recent_transactions,
        is_admin=is_admin
    )


# ============== MAKE CONTRIBUTION ==============
@wallet_bp.route('/groups/<int:group_id>/wallet/contribute', methods=['GET', 'POST'])
@login_required
def contribute(group_id):
    group = Group.query.get_or_404(group_id)
    wallet = group.wallet

    if not wallet:
        flash('This group does not have a wallet!', 'danger')
        return redirect(url_for('groups.view_group', group_id=group_id))

    # Check authorization
    allowed, reason = can_contribute(current_user.id, wallet.id)
    if not allowed:
        flash(reason, 'danger')
        return redirect(url_for('wallet.view_wallet', group_id=group_id))

    # Get user's ledger
    user_ledger = MemberLedger.query.filter_by(
        wallet_id=wallet.id,
        user_id=current_user.id
    ).first()

    if request.method == 'POST':
        try:
            amount = float(request.form.get('amount', 0))
            description = request.form.get('description', '')

            contribution, transaction = contribute_to_wallet(
                wallet_id=wallet.id,
                user_id=current_user.id,
                amount=amount,
                description=description
            )

            flash(f'Successfully contributed ₹{amount:.2f}!', 'success')
            return redirect(url_for('wallet.view_wallet', group_id=group_id))

        except InvalidAmountError as e:
            flash(str(e), 'danger')
        except DuplicateTransactionError as e:
            flash('This contribution was already processed.', 'warning')
        except WalletError as e:
            flash(str(e), 'danger')
        except ValueError:
            flash('Please enter a valid amount!', 'danger')

    return render_template(
        'wallet/contribute.html',
        group=group,
        wallet=wallet,
        user_ledger=user_ledger
    )


# ============== DISBURSE LOAN (Admin) ==============
@wallet_bp.route('/loans/<int:loan_id>/disburse', methods=['POST'])
@login_required
def disburse(loan_id):
    loan = LoanRequest.query.get_or_404(loan_id)

    try:
        transaction = disburse_loan(
            loan_id=loan_id,
            admin_user_id=current_user.id
        )

        flash(f'Loan of ₹{loan.approved_amount:.2f} disbursed successfully!', 'success')

    except InsufficientBalanceError as e:
        flash(str(e), 'danger')
    except AuthorizationError as e:
        flash(str(e), 'danger')
    except DuplicateTransactionError:
        flash('This loan has already been disbursed.', 'warning')
    except WalletError as e:
        flash(str(e), 'danger')

    return redirect(url_for('loans.view_loan', loan_id=loan_id))


# ============== APPROVE REPAYMENT (Admin) ==============
@wallet_bp.route('/repayments/<int:repayment_id>/approve', methods=['POST'])
@login_required
def approve_repayment_route(repayment_id):
    repayment = LoanRepayment.query.get_or_404(repayment_id)
    loan = repayment.loan

    try:
        repayment, transaction, distributions = approve_repayment(
            repayment_id=repayment_id,
            admin_user_id=current_user.id
        )

        flash(f'Repayment of ₹{repayment.amount:.2f} approved!', 'success')

        if loan.status == LoanStatus.COMPLETED.value:
            flash('🎉 Loan fully repaid and closed!', 'success')

        if distributions:
            flash(f'Interest distributed to {len(distributions)} lenders.', 'info')

    except AuthorizationError as e:
        flash(str(e), 'danger')
    except WalletError as e:
        flash(str(e), 'danger')

    return redirect(url_for('loans.view_loan', loan_id=loan.id))


# ============== REJECT REPAYMENT (Admin) ==============
@wallet_bp.route('/repayments/<int:repayment_id>/reject', methods=['POST'])
@login_required
def reject_repayment(repayment_id):
    repayment = LoanRepayment.query.get_or_404(repayment_id)
    loan = repayment.loan

    # Check authorization
    allowed, reason = can_approve_repayment(current_user.id, repayment_id)
    if not allowed:
        flash(reason, 'danger')
        return redirect(url_for('loans.view_loan', loan_id=loan.id))

    try:
        rejection_reason = request.form.get('reason', '')
        repayment.reject(current_user.id, rejection_reason)
        db.session.commit()

        flash('Repayment rejected.', 'warning')

    except ValueError as e:
        flash(str(e), 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Error: {str(e)}', 'danger')

    return redirect(url_for('loans.view_loan', loan_id=loan.id))


# ============== TRANSACTION HISTORY ==============
@wallet_bp.route('/groups/<int:group_id>/wallet/transactions')
@login_required
def transactions(group_id):
    group = Group.query.get_or_404(group_id)

    if not is_group_member(current_user.id, group_id):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    wallet = group.wallet
    if not wallet:
        flash('This group does not have a wallet!', 'danger')
        return redirect(url_for('groups.view_group', group_id=group_id))

    # Filter by type if provided
    type_filter = request.args.get('type', None)

    query = WalletTransaction.query.filter_by(
        wallet_id=wallet.id,
        is_reversed=False
    )

    if type_filter:
        query = query.filter_by(transaction_type=type_filter)

    # Pagination
    page = request.args.get('page', 1, type=int)
    per_page = 20

    transactions = query.order_by(
        WalletTransaction.created_at.desc()
    ).paginate(page=page, per_page=per_page, error_out=False)

    return render_template(
        'wallet/transactions.html',
        group=group,
        wallet=wallet,
        transactions=transactions,
        type_filter=type_filter,
        TransactionType=TransactionType
    )


# ============== MEMBER LEDGERS ==============
@wallet_bp.route('/groups/<int:group_id>/wallet/ledgers')
@login_required
def member_ledgers(group_id):
    group = Group.query.get_or_404(group_id)

    if not is_group_member(current_user.id, group_id):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    wallet = group.wallet
    if not wallet:
        flash('This group does not have a wallet!', 'danger')
        return redirect(url_for('groups.view_group', group_id=group_id))

    ledgers = MemberLedger.query.filter_by(wallet_id=wallet.id).all()

    # Calculate totals
    total_principal = sum(l.principal_contributed for l in ledgers)
    total_interest = sum(l.interest_earned for l in ledgers)

    return render_template(
        'wallet/ledgers.html',
        group=group,
        wallet=wallet,
        ledgers=ledgers,
        total_principal=total_principal,
        total_interest=total_interest
    )


# ============== RECALCULATE BALANCE (Admin) ==============
@wallet_bp.route('/groups/<int:group_id>/wallet/recalculate', methods=['POST'])
@login_required
def recalculate(group_id):
    group = Group.query.get_or_404(group_id)

    if not is_group_admin(current_user.id, group_id):
        flash('Only admin can recalculate wallet!', 'danger')
        return redirect(url_for('wallet.view_wallet', group_id=group_id))

    wallet = group.wallet
    if not wallet:
        flash('This group does not have a wallet!', 'danger')
        return redirect(url_for('groups.view_group', group_id=group_id))

    try:
        result = recalculate_wallet_balance(wallet.id)

        if result['was_corrected']:
            flash(
                f"Balance corrected! Previous: ₹{result['previous_balance']:.2f}, "
                f"New: ₹{result['calculated_balance']:.2f}",
                'warning'
            )
        else:
            flash('Wallet balance verified - no correction needed.', 'success')

    except WalletError as e:
        flash(str(e), 'danger')

    return redirect(url_for('wallet.view_wallet', group_id=group_id))


# ============== INTEREST DISTRIBUTIONS ==============
@wallet_bp.route('/groups/<int:group_id>/wallet/interest-distributions')
@login_required
def interest_distributions(group_id):
    group = Group.query.get_or_404(group_id)

    if not is_group_member(current_user.id, group_id):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    from app.models import InterestDistribution

    # Get user's distributions
    distributions = InterestDistribution.query.join(LoanRequest).filter(
        LoanRequest.group_id == group_id,
        InterestDistribution.beneficiary_id == current_user.id
    ).order_by(InterestDistribution.created_at.desc()).all()

    total_earned = sum(d.interest_earned for d in distributions)

    return render_template(
        'wallet/interest_distributions.html',
        group=group,
        distributions=distributions,
        total_earned=total_earned
    )