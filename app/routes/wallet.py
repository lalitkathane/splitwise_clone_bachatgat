"""
WALLET ROUTES
=============

Handles all wallet-related HTTP endpoints:
- View wallet details
- Make contributions
- Disburse loans
- Make repayments
- View transaction history
"""

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.extensions import db
from app.models import Group, GroupMember, GroupWallet, WalletTransaction, LoanRequest
from app.services.wallet_service import (
    contribute_to_wallet,
    disburse_loan,
    repay_loan,
    get_wallet_summary,
    get_member_contribution_total,
    get_pending_disbursements,
    get_active_loans,
    WalletError,
    InsufficientBalanceError,
    UnauthorizedError,
    InvalidAmountError
)

wallet_bp = Blueprint('wallet', __name__)


# ============== VIEW WALLET ==============
@wallet_bp.route('/groups/<int:group_id>/wallet')
@login_required
def view_wallet(group_id):
    """Display wallet details and summary."""
    group = Group.query.get_or_404(group_id)

    # Check membership
    if not group.is_member(current_user):
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
        return redirect(url_for('groups.view_group', group_id=group_id))

    # Get user's contribution total
    user_contribution = get_member_contribution_total(wallet.id, current_user.id)

    # Get pending disbursements (for admin)
    pending_disbursements = get_pending_disbursements(group_id)

    # Get active loans
    active_loans = get_active_loans(group_id)

    # Check if current user is admin
    is_admin = group.is_admin(current_user)

    # Get recent transactions
    recent_transactions = WalletTransaction.query.filter_by(
        wallet_id=wallet.id
    ).order_by(WalletTransaction.created_at.desc()).limit(10).all()

    return render_template(
        'wallet/view.html',
        group=group,
        wallet=wallet,
        summary=summary,
        user_contribution=user_contribution,
        pending_disbursements=pending_disbursements,
        active_loans=active_loans,
        is_admin=is_admin,
        recent_transactions=recent_transactions
    )


# ============== MAKE CONTRIBUTION ==============
@wallet_bp.route('/groups/<int:group_id>/wallet/contribute', methods=['GET', 'POST'])
@login_required
def contribute(group_id):
    """Handle member contributions to wallet."""
    group = Group.query.get_or_404(group_id)

    # Check membership
    if not group.is_member(current_user):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    wallet = group.wallet
    if not wallet:
        flash('This group does not have a wallet!', 'danger')
        return redirect(url_for('groups.view_group', group_id=group_id))

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

            flash(f'Successfully contributed ₹{amount:.2f} to the group wallet!', 'success')
            return redirect(url_for('wallet.view_wallet', group_id=group_id))

        except InvalidAmountError as e:
            flash(str(e), 'danger')
        except UnauthorizedError as e:
            flash(str(e), 'danger')
        except Exception as e:
            flash(f'Error making contribution: {str(e)}', 'danger')

    # Get user's current contribution total
    user_contribution = get_member_contribution_total(wallet.id, current_user.id)

    return render_template(
        'wallet/contribute.html',
        group=group,
        wallet=wallet,
        user_contribution=user_contribution
    )


# ============== DISBURSE LOAN (ADMIN ONLY) ==============
@wallet_bp.route('/loans/<int:loan_id>/disburse', methods=['POST'])
@login_required
def disburse(loan_id):
    """Disburse an approved loan (admin only)."""
    loan = LoanRequest.query.get_or_404(loan_id)
    group = loan.group

    # Check if user is admin
    if not group.is_admin(current_user):
        flash('Only group admin can disburse loans!', 'danger')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

    try:
        transaction = disburse_loan(
            loan_id=loan_id,
            disbursed_by_user_id=current_user.id
        )

        flash(f'Loan of ₹{loan.approved_amount:.2f} disbursed successfully!', 'success')

    except InsufficientBalanceError as e:
        flash(str(e), 'danger')
    except WalletError as e:
        flash(str(e), 'danger')
    except Exception as e:
        flash(f'Error disbursing loan: {str(e)}', 'danger')

    return redirect(url_for('loans.view_loan', loan_id=loan_id))


# ============== REPAY LOAN ==============
@wallet_bp.route('/loans/<int:loan_id>/repay', methods=['GET', 'POST'])
@login_required
def repay(loan_id):
    """Handle loan repayment by borrower."""
    loan = LoanRequest.query.get_or_404(loan_id)
    group = loan.group

    # Check if user is the borrower
    if loan.requested_by != current_user.id:
        flash('Only the borrower can repay this loan!', 'danger')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

    # Check if loan was disbursed
    if not loan.disbursed_at:
        flash('This loan has not been disbursed yet!', 'warning')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

    # Check if already fully repaid
    if loan.is_fully_repaid:
        flash('This loan has already been fully repaid!', 'info')
        return redirect(url_for('loans.view_loan', loan_id=loan_id))

    if request.method == 'POST':
        try:
            amount = float(request.form.get('amount', 0))
            description = request.form.get('description', '')

            repayment, transaction, is_fully_repaid = repay_loan(
                loan_id=loan_id,
                user_id=current_user.id,
                amount=amount,
                description=description
            )

            if is_fully_repaid:
                flash(f'Congratulations! Loan fully repaid with ₹{amount:.2f}!', 'success')
            else:
                remaining = loan.get_remaining_amount()
                flash(f'Repaid ₹{amount:.2f}. Remaining: ₹{remaining:.2f}', 'success')

            return redirect(url_for('loans.view_loan', loan_id=loan_id))

        except InvalidAmountError as e:
            flash(str(e), 'danger')
        except WalletError as e:
            flash(str(e), 'danger')
        except Exception as e:
            flash(f'Error making repayment: {str(e)}', 'danger')

    remaining_amount = loan.get_remaining_amount()

    return render_template(
        'wallet/repay.html',
        loan=loan,
        group=group,
        remaining_amount=remaining_amount
    )


# ============== TRANSACTION HISTORY ==============
@wallet_bp.route('/groups/<int:group_id>/wallet/transactions')
@login_required
def transactions(group_id):
    """View all wallet transactions."""
    group = Group.query.get_or_404(group_id)

    # Check membership
    if not group.is_member(current_user):
        flash('You are not a member of this group!', 'danger')
        return redirect(url_for('groups.list_groups'))

    wallet = group.wallet
    if not wallet:
        flash('This group does not have a wallet!', 'danger')
        return redirect(url_for('groups.view_group', group_id=group_id))

    # Get all transactions with pagination
    page = request.args.get('page', 1, type=int)
    per_page = 20

    transactions = WalletTransaction.query.filter_by(
        wallet_id=wallet.id
    ).order_by(
        WalletTransaction.created_at.desc()
    ).paginate(page=page, per_page=per_page, error_out=False)

    return render_template(
        'wallet/transactions.html',
        group=group,
        wallet=wallet,
        transactions=transactions
    )