"""Decision layer (CONTEXT.md 7.7) in integer pence. Pure functions, no I/O."""

ORDER = ("fight", "offer", "accept")  # tie-break order
NOT_RECEIVED = "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"


def decide(p: float, value_minor: int, stage: str, fee_minor: int, offer_share_pct: int, allowed=ORDER) -> dict:
    """Highest expected value among the allowed actions: fight (win p, else pay the fee), offer a
    refund share, or accept (refund everything, EV 0). The fee only applies at CHARGEBACK."""
    allowed = set(allowed)
    if not 0 <= p <= 1 or value_minor < 0 or not allowed or not allowed <= set(ORDER):
        raise ValueError("p must be in [0, 1], value non-negative, allowed a non-empty subset of fight/offer/accept")
    fee = fee_minor if stage == "CHARGEBACK" else 0
    ev = {"fight": p * value_minor - (1 - p) * fee, "offer": value_minor * (100 - offer_share_pct) / 100, "accept": 0}
    action = max((a for a in ORDER if a in allowed), key=ev.get)  # compared unrounded; max keeps the first of equals
    return {"action": action, "ev": {a: round(v) for a, v in ev.items()}, "offer_amount_minor": value_minor * offer_share_pct // 100}


def paypal_action(action: str, reason: str) -> str:
    """PayPal's link rel for a decision. Fighting means evidence only on not-received disputes (where the sandbox
    offered it), a message otherwise; escalate is never proposed."""
    if action == "fight":
        return "provide_evidence" if reason == NOT_RECEIVED else "send_message"
    return {"offer": "make_offer", "accept": "accept_claim"}[action]
