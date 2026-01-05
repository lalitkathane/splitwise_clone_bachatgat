# This makes 'services' a Python package
"""
Services Package
================

Business logic layer for Bachat Gat.

All financial and authorization operations are handled here.
Routes should call these services, not manipulate models directly.
"""

from app.services.wallet_service import (
    create_wallet_for_group,
    contribute_to_wallet,
    disburse_loan,
    submit_repayment,
    approve_repayment,
    recalculate_wallet_balance,
    get_wallet_summary,
    WalletError,
    InsufficientBalanceError,
    InvalidAmountError,
    DuplicateTransactionError
)

from app.services.authorization_service import (
    can_contribute,
    can_vote,
    can_disburse,
    can_repay,
    can_approve_repayment,
    can_leave_group,
    can_transfer_admin,
    is_group_member,
    is_group_admin,
    require_authorization,
    AuthorizationError
)

from app.services.loan_service import (
    create_loan_request,
    cast_vote,
    get_loan_details,
    LoanError
)

from app.services.membership_service import (
    add_member,
    leave_group,
    remove_member,
    transfer_admin,
    get_member_liabilities,
    MembershipError
)