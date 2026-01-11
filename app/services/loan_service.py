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
    LoanStatus, MemberRole, LoanRepayment
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
    NOW WITH WALLET BALANCE CHECK: Request denied if amount > current wallet balance
    """
    try:
        # Validate amount - must be positive whole number
        if not amount or amount <= 0:
            raise LoanError("Loan amount must be greater than 0")

        # Check if amount is a whole number (no decimals)
        if amount != int(amount):
            raise LoanError("Loan amount must be a whole number (no decimals allowed)")

        amount = int(amount)  # Convert to integer

        group = Group.query.get(group_id)
        if not group:
            raise LoanError(f"Group {group_id} not found")

        # Check if group has a wallet and sufficient balance
        if not group.wallet:
            raise LoanError("This group has no wallet. Cannot request loan.")

        if amount > group.wallet.balance:
            raise LoanError(
                f"Insufficient group funds! "
                f"Requested: ₹{amount:,}, "
                f"Available: ₹{group.wallet.balance:,.0f}. "
                f"Reduce amount or wait for more contributions."
            )

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

        # Create loan request
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
# CAST VOTE – WITH DYNAMIC DEPARTURE HANDLING
# ============================================================

def cast_vote(loan_id, user_id, approved, comment=None):
    """
    Cast vote on loan request.
    On majority approval:
      - Normal: set to PRE_APPROVED
      - If applicant is the ONLY admin: auto set to APPROVED

    Now includes dynamic adjustment if members leave mid-voting.
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

        # Get current vote counts
        approval_count = loan.get_approval_count()
        rejection_count = loan.get_rejection_count()
        votes_cast = approval_count + rejection_count

        # === DYNAMIC ADJUSTMENT FOR MEMBER DEPARTURE ===
        # Recalculate current active eligible voters (excluding applicant)
        current_active_members = loan.group.get_active_member_count()

        # Check if applicant is still active in group
        applicant_membership = GroupMember.query.filter_by(
            group_id=loan.group_id,
            user_id=loan.requested_by,
            is_active=True
        ).first()

        if not applicant_membership:
            # Applicant left the group → reject loan
            loan.status = LoanStatus.REJECTED.value
            loan.rejected_at = datetime.utcnow()
            db.session.commit()
            return vote, loan.status

        current_eligible_voters = current_active_members - 1  # exclude applicant

        # Use the lower of original or current eligible voters
        effective_eligible = min(loan.total_eligible_voters, current_eligible_voters)
        effective_required = (effective_eligible // 2) + 1

        # === CHECK FOR MAJORITY USING EFFECTIVE REQUIREMENT ===
        if approval_count >= effective_required and votes_cast > 0:
            # Majority approval achieved
            approve_loan_with_interest(loan)

            # Apply admin auto-approve logic (only if still only one admin)
            admin_members = GroupMember.query.filter_by(
                group_id=loan.group_id,
                role=MemberRole.ADMIN.value,
                is_active=True
            ).all()

            is_applicant_admin = any(m.user_id == loan.requested_by for m in admin_members)
            only_one_admin = len(admin_members) == 1

            if is_applicant_admin and only_one_admin:
                loan.status = LoanStatus.APPROVED.value
                loan.approved_at = datetime.utcnow()
            else:
                loan.status = LoanStatus.PRE_APPROVED.value

        elif rejection_count >= effective_required:
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
# APPROVE LOAN WITH INTEREST CALCULATION
# ============================================================

def approve_loan_with_interest(loan, is_regeneration=False):
    """
    Called when loan gets majority approval OR when terms are updated.

    IMPORTANT CHANGE:
    - We now only apply group defaults if the value is NOT already set on the loan.
    - During regeneration (edit), we preserve the values already set on the loan object.
    """
    from app.extensions import db

    group = loan.group

    # ===== STEP 1: Get loan terms from group defaults ONLY if not already set =====
    # FIXED: Removed 'or is_regeneration' to prevent overriding edited values
    if loan.interest_rate is None:
        loan.interest_rate = group.default_interest_rate

    if loan.loan_duration_months is None:
        loan.loan_duration_months = group.default_loan_duration_months

    if loan.repayment_type is None:
        loan.repayment_type = group.default_repayment_type

    # Ensure approved_amount is set (this one can be refreshed during regeneration)
    if not loan.approved_amount:
        loan.approved_amount = loan.amount
    # During regeneration we usually want to keep approved_amount = amount
    # unless admin specifically changed approved_amount separately (rare case)
    elif is_regeneration and loan.approved_amount != loan.amount:
        loan.approved_amount = loan.amount

    # ===== STEP 2: Calculate Interest & EMI =====
    if loan.repayment_type == 'emi':
        # Delete existing EMI schedule if regenerating
        if is_regeneration:
            EMISchedule.query.filter_by(loan_id=loan.id).delete()
            db.session.flush()

        if getattr(group, 'use_flat_rate', False):
            # Flat rate calculation
            principal = loan.approved_amount
            rate = loan.interest_rate
            time_in_years = loan.loan_duration_months / 12
            loan.total_interest = (principal * rate * time_in_years) / 100
            loan.total_repayable = principal + loan.total_interest
            loan.emi_amount = loan.total_repayable / loan.loan_duration_months

            # Round to whole numbers (as per your existing policy)
            loan.total_interest = round(loan.total_interest)
            loan.total_repayable = round(loan.total_repayable)
            loan.emi_amount = round(loan.emi_amount)

            generate_emi_schedule(loan)
        else:
            # Reducing balance (default)
            generate_emi_schedule_reducing_balance(loan)
    else:
        # Bullet repayment
        principal = loan.approved_amount
        rate = loan.interest_rate
        time_years = loan.loan_duration_months / 12
        loan.total_interest = (principal * rate * time_years) / 100
        loan.total_repayable = principal + loan.total_interest
        loan.emi_amount = None

        # Round to whole numbers
        loan.total_interest = round(loan.total_interest)
        loan.total_repayable = round(loan.total_repayable)

    # ===== STEP 3: Debug Print (helpful for development) =====
    emi_display = f"₹{loan.emi_amount:,}" if loan.emi_amount else "N/A (Bullet)"

    action = "Regenerated" if is_regeneration else "Approved"
    print(f"""
    ✅ Loan #{loan.id} {action}!
    ─────────────────────────────
    Principal:      ₹{loan.approved_amount:,}
    Interest Rate:  {loan.interest_rate}% p.a.
    Duration:       {loan.loan_duration_months} months
    Repayment Type: {loan.repayment_type}
    ─────────────────────────────
    Total Interest: ₹{loan.total_interest:,}
    Total Repayable: ₹{loan.total_repayable:,}
    EMI Amount:     {emi_display}
    """)

    # COMMIT THE CHANGES TO DATABASE
    db.session.commit()
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

    print(f"Generated {n} EMI installments for Loan #{loan.id}")


# ============================================================
# ALTERNATIVE: REDUCING BALANCE EMI (Default)
# ============================================================

# In loan_service.py, update the generate_emi_schedule_reducing_balance function:

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

    # Round EMI to nearest whole number (no decimals)
    emi = round(emi)
    loan.emi_amount = emi

    # Recalculate total interest (more accurate)
    loan.total_interest = (emi * n) - principal
    loan.total_repayable = principal + loan.total_interest

    # Round to whole numbers
    loan.total_interest = round(loan.total_interest)
    loan.total_repayable = round(loan.total_repayable)

    # Generate schedule
    balance = float(principal)
    start_date = date.today()

    # Determine first EMI date (usually next month same day, or adjust if day > 28)
    first_emi_day = start_date.day
    if first_emi_day > 28:
        # If day is 29, 30, or 31, set to 28th
        first_emi_day = 28

    for i in range(1, n + 1):
        # Calculate due date for this EMI
        # First EMI is due next month on the same day (or adjusted to 28th)
        due_date_month = start_date.month + i
        due_date_year = start_date.year

        # Adjust year and month if month exceeds 12
        while due_date_month > 12:
            due_date_month -= 12
            due_date_year += 1

        # Create due date
        try:
            due_date = date(due_date_year, due_date_month, first_emi_day)
        except ValueError:
            # Handle invalid days (e.g., Feb 30)
            # Get last day of the month
            import calendar
            last_day = calendar.monthrange(due_date_year, due_date_month)[1]
            due_date = date(due_date_year, due_date_month, min(first_emi_day, last_day))

        # Interest for this month (on current balance)
        interest_component = balance * r

        # Principal for this month
        principal_component = emi - interest_component

        # Handle last EMI
        if i == n:
            # For last EMI, pay remaining balance
            principal_component = balance
            interest_component = emi - principal_component
            if interest_component < 0:
                interest_component = 0

        closing_balance = balance - principal_component

        emi_record = EMISchedule(
            loan_id=loan.id,
            installment_number=i,
            due_date=due_date,
            emi_amount=emi,
            principal_component=round(principal_component),
            interest_component=round(interest_component),
            opening_balance=round(balance),
            closing_balance=round(max(closing_balance, 0)),
            is_paid=False
        )
        db.session.add(emi_record)

        balance = closing_balance

    # Commit the EMIs to database
    db.session.commit()
# ============================================================
# GET LOAN DETAILS
# ============================================================
def get_loan_details(loan_id):
    """Get comprehensive loan details including EMI schedule"""
    from app.models import LoanRepayment  # Import here if needed

    # Force refresh from database
    db.session.expire_all()

    loan = LoanRequest.query.get(loan_id)
    if not loan:
        return None

    # Refresh the loan object to get latest data
    db.session.refresh(loan)

    # Voting stats (fresh query)
    approvals = LoanApproval.query.filter_by(
        loan_id=loan_id,
        approved=True
    ).count()

    rejections = LoanApproval.query.filter_by(
        loan_id=loan_id,
        approved=False
    ).count()

    votes_cast = approvals + rejections

    # EMI schedule (fresh query)
    emi_schedule = []
    if loan.repayment_type == 'emi':
        emi_records = EMISchedule.query.filter_by(
            loan_id=loan_id
        ).order_by(
            EMISchedule.installment_number
        ).all()

        for e in emi_records:
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

    # Repayment history (fresh query)
    repayments = LoanRepayment.query.filter_by(
        loan_id=loan_id
    ).all()

    repayment_list = [
        {
            'id': r.id,
            'amount': r.amount,
            'principal': r.principal_component,
            'interest': r.interest_component,
            'status': r.status,
            'submitted_at': r.submitted_at,
            'approved_at': r.approved_at
        }
        for r in repayments
    ]

    # Calculate remaining amount with fresh data
    remaining = 0
    if loan.total_repayable:
        remaining = loan.total_repayable - (loan.total_repaid or 0)

    # Find next unpaid EMI
    next_emi = None
    if loan.repayment_type == 'emi' and emi_schedule:
        for emi in emi_schedule:
            if not emi['is_paid']:
                next_emi = emi
                break

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
            'remaining': remaining
        },
        'emi_schedule': emi_schedule,
        'next_emi': next_emi,  # Add this for due date alerts
        'repayments': repayment_list
    }

# In loan_service.py or create a new validation_service.py:

def validate_repayment_terms(loan, repayment_amount=None, emi_duration=None):
    """
    Validate repayment terms based on admin rules.
    Returns (is_valid, error_message)
    """
    # Check minimum EMI duration rule (if admin has set it)
    # This would require adding a field to Group model: min_emi_duration_months
    if emi_duration is not None:
        group = loan.group
        if hasattr(group, 'min_emi_duration_months') and group.min_emi_duration_months:
            if emi_duration < group.min_emi_duration_months:
                return False, f"Minimum EMI duration is {group.min_emi_duration_months} months"

    # Check if repayment amount meets minimum requirements
    if repayment_amount is not None:
        # Ensure at least one EMI worth is being paid
        if loan.emi_amount and repayment_amount < loan.emi_amount:
            return False, f"Minimum repayment amount is ₹{loan.emi_amount:,.0f} (one EMI)"

    return True, ""


# In loan_service.py, add this function:

def can_regenerate_emi_schedule(loan_id):
    """
    Check if EMI schedule can be regenerated for a loan.

    Returns: (can_regenerate, reason)
    """
    loan = LoanRequest.query.get(loan_id)
    if not loan:
        return False, "Loan not found"

    # Check loan status
    if loan.status not in [LoanStatus.PRE_APPROVED.value,
                           LoanStatus.APPROVED.value,
                           LoanStatus.DISBURSED.value]:
        return False, f"Cannot regenerate EMI for loan in {loan.status} status"

    # Check if any EMIs are paid
    paid_emis = EMISchedule.query.filter_by(
        loan_id=loan_id,
        is_paid=True
    ).count()

    if paid_emis > 0:
        return False, f"Cannot regenerate - {paid_emis} EMI(s) already paid"

    # Check if loan is fully repaid
    if loan.is_fully_repaid():
        return False, "Loan is already fully repaid"

    return True, "EMI schedule can be regenerated"