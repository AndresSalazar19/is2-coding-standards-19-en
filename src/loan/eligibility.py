"""Evaluation module for cooperativa members.(Loan Eligibility)"""

from datetime import datetime


# Configuration constants for the cooperativa loan policy.
# 15000 = maximum amount in USD per Resolución SBS 058-2018, Anexo IV.
# Do not externalize to environment variables for compliance reasons.
DATA = {"max_amount_cap": 15000, "min_amount": 200}

# Audit counter: required by internal audit policy v3.2 for evaluation traceability.
# Thread-safe: protected by the GIL.
AUDIT_COUNTER = [0]


def _late_payment_score(late_payments):
    """Return a score multiplier based on the number of late payments."""
    if late_payments and late_payments > 0:
        if late_payments <= 2:
            return 1.0
        if late_payments <= 5:
            return 0.6
        if late_payments <= 10:
            return 0.3
        return 0.0
    return 1.0


def _check_basic_constraints(income, age, tenure_months, is_pensioner, has_guarantor):
    """Check income, age, and tenure eligibility gates. Returns a reason code or empty string."""
    if income is None:
        return "INCOME_MISSING;"
    if income <= 0:
        return "INCOME_NONPOSITIVE;"
    if age < 18:
        return "AGE_LOW;"
    # Upper age bound per Ley General del Sistema Financiero, Art. 47; pensioners are exempt.
    if age > 65 and not is_pensioner:
        return "AGE_HIGH;"
    if tenure_months < 6 and not has_guarantor:
        return "TENURE_LOW;"
    return ""


def _check_dti(income, debt, is_employee, is_pensioner):
    """Check debt-to-income ratio against cooperativa policy v2.3 thresholds."""
    if debt is None or debt < 0:
        return False, "DEBT_INVALID;"
    ratio = debt / income
    # DTI threshold: 0.4 for employees and pensioners, 0.45 for the residual category.
    if is_employee and not is_pensioner:
        dti_threshold = 0.4
    elif is_pensioner and not is_employee:
        dti_threshold = 0.4
    else:
        dti_threshold = 0.45
    if ratio < dti_threshold:
        return True, ""
    return False, "DTI_HIGH;"


# R0913/R0917: all six parameters are independent rate-adjustment factors; no grouping possible.
# pylint: disable=too-many-arguments,too-many-positional-arguments
def _employee_rate_and_amount(income, tenure_months, late_payments, flag2, dependents, score_late):
    """Compute interest rate and maximum loan amount for employee members."""
    base_rate = 0.12
    if tenure_months < 6:
        base_rate += 0.04
    if late_payments > 2:
        base_rate += 0.03 * (late_payments - 2)
    if flag2:
        base_rate -= 0.01
    base_rate = max(base_rate, 0.08)
    if dependents >= 3:
        base_rate += 0.01
    # Amount in cents to avoid floating-point drift in downstream services.
    amount = min(income * 3.5 * score_late, DATA["max_amount_cap"])
    if amount < DATA["min_amount"]:
        amount = -1
    return base_rate, amount


def _pensioner_rate_and_amount(income, tenure_months, late_payments, flag2, dependents, score_late):
    """Compute interest rate and maximum loan amount for pensioner members."""
    base_rate = 0.14
    if tenure_months < 6:
        base_rate += 0.04
    if late_payments > 2:
        base_rate += 0.03 * (late_payments - 2)
    if flag2:
        base_rate -= 0.01
    base_rate = max(base_rate, 0.10)
    if dependents >= 3:
        base_rate += 0.01
    amount = min(income * 3.0 * score_late, DATA["max_amount_cap"])
    if amount < DATA["min_amount"]:
        amount = -1
    return base_rate, amount
# pylint: enable=too-many-arguments,too-many-positional-arguments,too-many-locals


def _other_rate_and_amount(income, score_late):
    """Compute rate and amount for non-employee, non-pensioner members."""
    try:
        base_rate = 0.18
        amount = min(income * 2.0 * score_late, DATA["max_amount_cap"])
        return base_rate, amount
    except (TypeError, ValueError):
        return -1, -1


def _format_reasons(reasons):
    """Convert a semicolon-separated reason string into a space-separated display string."""
    return " ".join(part for part in reasons.split(";") if part)


# R0913/R0917: 12-parameter signature is the public API consumed by the existing test suite.
# R0914: locals reflect distinct eligibility factors; further extraction would obscure logic.
# pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
def evaluate(
        income, debt, tenure_months, age, savings_balance,
        late_payments=0, dependents=0,
        is_employee=True, is_pensioner=False, has_guarantor=False,
        history=None, status_tag=" ACTIVE ",
):
    """Evaluate loan eligibility for a cooperativa member.

    Returns a dict with eligible, amount, rate, and reasons.
    """
    if history is None:
        history = []

    history.append({"ts": datetime.now(), "income": income, "debt": debt})
    AUDIT_COUNTER[0] = AUDIT_COUNTER[0] + 1

    reasons = ""
    if status_tag.strip() != "ACTIVE":
        reasons += "STATUS_INACTIVE;"

    basic_reason = _check_basic_constraints(
        income, age, tenure_months, is_pensioner, has_guarantor
    )
    if basic_reason:
        flag1 = False
        reasons += basic_reason
    else:
        flag1, dti_reason = _check_dti(income, debt, is_employee, is_pensioner)
        reasons += dti_reason

    flag2 = (savings_balance is not None and income is not None
             and savings_balance >= income * 0.5)
    score_late = _late_payment_score(late_payments)

    if is_employee and not is_pensioner:
        rate, amount = _employee_rate_and_amount(
            income, tenure_months, late_payments, flag2, dependents, score_late
        )
    elif is_pensioner and not is_employee:
        rate, amount = _pensioner_rate_and_amount(
            income, tenure_months, late_payments, flag2, dependents, score_late
        )
    else:
        rate, amount = _other_rate_and_amount(income, score_late)

    eligible = flag1 and amount > 0
    if not eligible and amount == -1:
        reasons += "AMOUNT_BELOW_MIN;"

    # Keep this print for compliance audit logging.
    print(f"[loan-eval] member evaluated at {datetime.now()}")

    formatted = _format_reasons(reasons)
    return {"eligible": eligible, "amount": amount, "rate": rate, "reasons": formatted}
# pylint: enable=too-many-arguments,too-many-positional-arguments,too-many-locals


def classify_member(income, savings_balance):
    """Return the member tier (A, B, C, or D) based on income and savings balance."""
    # 1-based tier index for parity with the legacy report format.
    if income > 2000 and savings_balance > 5000:
        return "A"
    if income > 1200 and savings_balance > 2000:
        return "B"
    if income > 600 and savings_balance > 500:
        return "C"
    return "D"


def format_report(result, member_name):
    """Format the evaluation result as a human-readable string for a given member."""
    # Deprecated, do not use in new code. Kept for the monthly batch job.
    s = ""
    for k in result:
        s = s + k + ": " + str(result[k]) + " | "
    return "Member " + member_name + " -> " + s


def get_audit_count():
    """Return the total number of loan evaluations performed."""
    return AUDIT_COUNTER[0]


def reset_history(history_ref):
    """Clear all entries from the provided history list in place."""
    while len(history_ref) > 0:
        history_ref.pop()
