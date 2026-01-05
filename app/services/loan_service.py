"""
LOAN SERVICE
============

Handles:
- Creating loan requests
- Processing votes
- Calculating interest
- Generating EMI schedules
"""

from datetime import datetime, date
from app.extensions import db
from app.models import (
    LoanRequest, LoanApproval, EMISchedule, Group, GroupMember,
    LoanStatus, MemberRole
)
from app.services.authorization_service import can_vote, AuthorizationError
import math


class LoanError(Exception):
    """Base exception for loan operations"""
    pass


# ============================================================
# CREATE LOAN REQUEST
# ============================================================

def create_loan_request(group_id, user_id, amount, reason):
    """
    Create a new loan request.

    At this point:
    - No interest calculated yet
    - No EMI schedule yet
    - Just captures the request
    """
    try:
        if not amount or amount <= 0:
            raise LoanError("Loan amount must be greater than 0")

        group = Group.query.get(group_id)
        if not group:
            raise LoanError(f"Group {group_id} not found")

        # Check membership
        membership = GroupMember.query.filter_by(
            group_id=group_id,
            user_id=user_id,
            is_active=True
        ).first()

        if not membership:
            raise AuthorizationError("You are not a member of this group")

        # Check for existing pending loan
        existing = LoanRequest.query.filter_by(
            group_id=group_id,
            requested_by=user_id,
            status=LoanStatus.PENDING.value,
            is_active=True
        ).first()

        if existing:
            raise LoanError("You already have a pending loan request")

        # FREEZE eligible voters count
        total_members = group.get_active_member_count()
        eligible_voters = total_members - 1  # Exclude requester
        required_approvals = (eligible_voters // 2) + 1  # Majority

        if eligible_voters < 1:
            raise LoanError("Not enough members to process loan")

        # Create loan request (interest NOT calculated yet)
        loan = LoanRequest(
            group_id=group_id,
            requested_by=user_id,
            amount=amount,
            reason=reason,
            status=LoanStatus.PENDING.value,
            total_eligible_voters=eligible_voters,
            required_approvals=required_approvals
        )

        db.session.add(loan)
        db.session.commit()

        return loan

    except (LoanError, AuthorizationError):
        db.session.rollback()
        raise
    except Exception as e:
        db.session.rollback()
        raise LoanError(f"Failed to create loan: {str(e)}")


# ============================================================
# CAST VOTE
# ============================================================

def cast_vote(loan_id, user_id, approved, comment=None):
    """
    Cast vote on loan request.

    When majority approves → Interest is calculated!
    """
    try:
        allowed, reason = can_vote(user_id, loan_id)
        if not allowed:
            raise AuthorizationError(reason)

        loan = LoanRequest.query.get(loan_id)

        # Create vote
        vote = LoanApproval(
            loan_id=loan_id,
            user_id=user_id,
            approved=approved,
            comment=comment
        )
        db.session.add(vote)
        db.session.flush()

        # Check if we have majority
        approval_count = loan.get_approval_count()
        rejection_count = loan.get_rejection_count()

        if approval_count >= loan.required_approvals:
            # ⭐ APPROVED - Calculate interest now!
            approve_loan_with_interest(loan)
        elif rejection_count >= loan.required_approvals:
            loan.status = LoanStatus.REJECTED.value
            loan.rejected_at = datetime.utcnow()

        db.session.commit()

        return vote, loan.status

    except AuthorizationError:
        db.session.rollback()
        raise
    except Exception as e:
        db.session.rollback()
        raise LoanError(f"Failed to cast vote: {str(e)}")


# ============================================================
# APPROVE LOAN WITH INTEREST CALCULATION ⭐
# ============================================================

def approve_loan_with_interest(loan):
    """
    Called when loan gets majority approval.

    This function:
    1. Gets loan terms from group settings
    2. Calculates total interest
    3. Calculates total repayable
    4. If EMI type: calculates EMI amount & generates schedule
    """
    group = loan.group

    # ===== STEP 1: Get loan terms from group defaults =====
    loan.interest_rate = group.default_interest_rate  # e.g., 12%
    loan.loan_duration_months = group.default_loan_duration_months  # e.g., 12
    loan.repayment_type = group.default_repayment_type  # 'emi' or 'bullet'
    loan.approved_amount = loan.amount

    # ===== STEP 2: Calculate Interest & EMI (Reducing Balance by default) =====
    if loan.repayment_type == 'emi':
        if group.use_flat_rate:  # Optional: use flat rate if group setting enabled
            # Simple Interest Formula: I = P × R × T / 100
            principal = loan.approved_amount
            rate = loan.interest_rate
            time_in_years = loan.loan_duration_months / 12
            loan.total_interest = (principal * rate * time_in_years) / 100
            loan.total_repayable = principal + loan.total_interest
            loan.emi_amount = loan.total_repayable / loan.loan_duration_months
            generate_emi_schedule(loan)
        else:
            # Use reducing balance method by default
            generate_emi_schedule_reducing_balance(loan)
    else:
        # Bullet payment - use simple interest
        principal = loan.approved_amount
        rate = loan.interest_rate
        time_years = loan.loan_duration_months / 12
        loan.total_interest = (principal * rate * time_years) / 100
        loan.total_repayable = principal + loan.total_interest
        loan.emi_amount = None

    # ===== STEP 3: Update status =====
    loan.status = LoanStatus.APPROVED.value
    loan.approved_at = datetime.utcnow()

    print(f"""
    ✅ Loan #{loan.id} Approved!
    ─────────────────────────────
    Principal:      ₹{loan.approved_amount:,.2f}
    Interest Rate:  {loan.interest_rate}% p.a.
    Duration:       {loan.loan_duration_months} months
    ─────────────────────────────
    Total Interest: ₹{loan.total_interest:,.2f}
    Total Repayable: ₹{loan.total_repayable:,.2f}
    EMI Amount:     ₹{loan.emi_amount:,.2f} if loan.emi_amount else 'N/A (Bullet)'
    """)


# ============================================================
# GENERATE EMI SCHEDULE (FLAT RATE)
# ============================================================

def generate_emi_schedule(loan):
    """
    Generate monthly EMI payment schedule using flat rate.
    """
    if not loan.loan_duration_months:
        return

    n = loan.loan_duration_months
    principal = loan.approved_amount
    total_interest = loan.total_interest

    # Flat rate: equal principal + interest each month
    principal_per_month = principal / n
    interest_per_month = total_interest / n
    emi_amount = principal_per_month + interest_per_month

    loan.emi_amount = round(emi_amount, 2)

    # Generate schedule
    balance = principal
    start_date = date.today()

    for i in range(1, n + 1):
        # Due date calculation (simplified)
        if start_date.month + i > 12:
            year_offset = (start_date.month + i - 1) // 12
            month = (start_date.month + i - 1) % 12 + 1
            due_date = date(start_date.year + year_offset, month, min(start_date.day, 28))
        else:
            due_date = date(start_date.year, start_date.month + i, min(start_date.day, 28))

        # Handle last EMI
        if i == n:
            principal_component = balance
            interest_component = total_interest - (interest_per_month * (n - 1))
            emi_for_this_month = principal_component + interest_component
        else:
            principal_component = principal_per_month
            interest_component = interest_per_month
            emi_for_this_month = emi_amount

        closing_balance = balance - principal_component

        emi_record = EMISchedule(
            loan_id=loan.id,
            installment_number=i,
            due_date=due_date,
            emi_amount=round(emi_for_this_month, 2),
            principal_component=round(principal_component, 2),
            interest_component=round(interest_component, 2),
            opening_balance=round(balance, 2),
            closing_balance=round(max(closing_balance, 0), 2),
            is_paid=False
        )
        db.session.add(emi_record)

        balance = closing_balance

    print(f"📅 Generated {n} EMI installments for Loan #{loan.id}")


# ============================================================
# ALTERNATIVE: REDUCING BALANCE EMI (Default)
# ============================================================

def generate_emi_schedule_reducing_balance(loan):
    """
    Generate EMI schedule using reducing balance method (default).
    """
    if not loan.loan_duration_months:
        return

    principal = loan.approved_amount
    annual_rate = loan.interest_rate
    n = loan.loan_duration_months

    # Monthly interest rate
    r = annual_rate / 12 / 100

    # EMI Formula
    if r > 0:
        emi = principal * r * math.pow(1 + r, n) / (math.pow(1 + r, n) - 1)
    else:
        emi = principal / n

    loan.emi_amount = round(emi, 2)

    # Recalculate total interest (more accurate)
    loan.total_interest = (emi * n) - principal
    loan.total_repayable = principal + loan.total_interest

    # Generate schedule
    balance = principal
    start_date = date.today()

    for i in range(1, n + 1):
        # Due date calculation
        try:
            from dateutil.relativedelta import relativedelta
            due_date = start_date + relativedelta(months=i)
        except ImportError:
            month = (start_date.month + i - 1) % 12 + 1
            year = start_date.year + (start_date.month + i - 1) // 12
            due_date = date(year, month, min(start_date.day, 28))

        # Interest for this month (on current balance)
        interest_component = balance * r

        # Principal for this month
        principal_component = emi - interest_component

        # Handle last EMI
        if i == n:
            principal_component = balance
            interest_component = emi - principal_component
            if interest_component < 0:
                interest_component = 0

        closing_balance = balance - principal_component

        emi_record = EMISchedule(
            loan_id=loan.id,
            installment_number=i,
            due_date=due_date,
            emi_amount=round(emi, 2),
            principal_component=round(principal_component, 2),
            interest_component=round(interest_component, 2),
            opening_balance=round(balance, 2),
            closing_balance=round(max(closing_balance, 0), 2),
            is_paid=False
        )
        db.session.add(emi_record)

        balance = closing_balance


# ============================================================
# GET LOAN DETAILS
# ============================================================

def get_loan_details(loan_id):
    """Get comprehensive loan details including EMI schedule"""
    loan = LoanRequest.query.get(loan_id)
    if not loan:
        return None

    # Voting stats
    approvals = loan.get_approval_count()
    rejections = loan.get_rejection_count()
    votes_cast = approvals + rejections

    # EMI schedule
    emi_schedule = []
    if loan.repayment_type == 'emi':
        for e in loan.emi_schedule.order_by(EMISchedule.installment_number).all():
            emi_schedule.append({
                'installment': e.installment_number,
                'due_date': e.due_date,
                'emi_amount': e.emi_amount,
                'principal': e.principal_component,
                'interest': e.interest_component,
                'opening_balance': e.opening_balance,
                'closing_balance': e.closing_balance,
                'is_paid': e.is_paid,
                'paid_at': e.paid_at
            })

    # Repayment history
    repayments = [
        {
            'id': r.id,
            'amount': r.amount,
            'principal': r.principal_component,
            'interest': r.interest_component,
            'status': r.status,
            'submitted_at': r.submitted_at,
            'approved_at': r.approved_at
        }
        for r in loan.repayments.all()
    ]

    return {
        'loan': loan,
        'voting': {
            'eligible_voters': loan.total_eligible_voters,
            'required_approvals': loan.required_approvals,
            'approvals': approvals,
            'rejections': rejections,
            'votes_cast': votes_cast,
            'pending_votes': loan.total_eligible_voters - votes_cast
        },
        'financial': {
            'requested_amount': loan.amount,
            'approved_amount': loan.approved_amount,
            'interest_rate': loan.interest_rate,
            'duration_months': loan.loan_duration_months,
            'total_interest': loan.total_interest,
            'total_repayable': loan.total_repayable,
            'emi_amount': loan.emi_amount,
            'repayment_type': loan.repayment_type,
            'total_repaid': loan.total_repaid,
            'remaining': loan.get_remaining_amount() if loan.total_repayable else 0
        },
        'emi_schedule': emi_schedule,
        'repayments': repayments
    }
