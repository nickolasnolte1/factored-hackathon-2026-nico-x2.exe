"""Which Silver anchor each paraphrase family needs.

A family whose templates carry slots is rendered from a real transaction (and its product and customer) that fits
the wording: an ATM family needs an ATM withdrawal, a declined-card family a declined purchase, a fee family a
bank-initiated Adjustment (the only fee-like movement type in Silver; cards and accounts have none). Every family
with slots must be listed; generate.py refuses to run otherwise.

refs=True means the text points at that specific movement, so the row's anchor carries its transaction_id (the gold
for transaction resolution). refs=False rows keep the customer and product but no transaction_id.
free_date=True means {date} is not a transaction date (for example the day a card was stolen).
same_day=True means the text says the movement just happened (an alert that just arrived), so the row's clock is
minutes after the transaction instead of days.
"""
from dataclasses import dataclass

CARDS = frozenset({"Credit Card", "Debit Card"})
ACCOUNTS = frozenset({"Savings Account", "Checking Account"})
CREDIT = frozenset({"Credit Card", "Personal Loan", "Mortgage"})
SUBSCRIPTIONS = frozenset({"Streaming Music", "Cable TV", "Internet Plus", "Empresa Telefónica"})
STORES = frozenset({"Super Ahorro", "Tienda Don José", "Mercado Central", "Ferretería", "Tienda General", "Boutique Moda",
                    "Centro Comercial", "Farmacia Salud", "Óptica Visión"})   # shops where goods are returned or ordered
IN_PERSON = STORES | {"Restaurante El Buen Sabor", "Estación de Servicio", "Gasolinera Express", "Taxi Seguro",
                      "Cine Premium", "Teatro Nacional", "Conciertos Live", "Clínica Médica", "Laboratorio Central"}
NARRATABLE_CHANNELS = frozenset({"ATM", "App", "Branch", "POS", "Web"})
DEBIT_KINDS = ("purchase_approved", "withdrawal_approved", "transfer_approved", "payment_approved")


@dataclass(frozen=True)
class AnchorSpec:
    kinds: tuple = ()
    products: frozenset = None
    channels: frozenset = None
    merchants: frozenset = None
    types: frozenset = None
    max_usd: float = None
    intl: bool = None
    refs: bool = True
    free_date: bool = False
    same_day: bool = False
    synth: str = None          # synthetic amount for families without an anchor: refund | financing

    @property
    def anchored(self):
        return bool(self.kinds)


P = AnchorSpec(kinds=("purchase_approved",))
P_SUBS = AnchorSpec(kinds=("purchase_approved",), merchants=SUBSCRIPTIONS)
P_SMALL = AnchorSpec(kinds=("purchase_approved",), max_usd=60)
P_CC = AnchorSpec(kinds=("purchase_approved",), products=frozenset({"Credit Card"}))
P_NOW = AnchorSpec(kinds=("purchase_approved",), same_day=True)                      # "the alert just arrived"
P_SMALL_NOW = AnchorSpec(kinds=("purchase_approved",), max_usd=60, same_day=True)
P_STORE = AnchorSpec(kinds=("purchase_approved",), merchants=STORES)                   # returned or cancelled goods
P_DELIVERY = AnchorSpec(kinds=("purchase_approved",), merchants=STORES | {"Restaurante El Buen Sabor"})  # never delivered
P_CC_INSTALMENTS = AnchorSpec(kinds=("purchase_approved",), products=frozenset({"Credit Card"}),
                              merchants=STORES | {"Clínica Médica"})
P_SKIMMED = AnchorSpec(kinds=("purchase_approved",), channels=frozenset({"POS"}), merchants=IN_PERSON)  # card terminal
P_INTL = AnchorSpec(kinds=("purchase_approved",), intl=True)
P_DOMESTIC = AnchorSpec(kinds=("purchase_approved",), intl=False)
P_STATUS = AnchorSpec(kinds=("purchase_approved", "purchase_pending"))
P_REV = AnchorSpec(kinds=("purchase_reversed",))
P_DECL = AnchorSpec(kinds=("purchase_declined",))
ATM_W = AnchorSpec(kinds=("withdrawal_approved",), channels=frozenset({"ATM"}))
ACCOUNT_DEBIT = AnchorSpec(kinds=("withdrawal_approved", "transfer_approved"), products=ACCOUNTS)
TRANSFER = AnchorSpec(kinds=("transfer_approved",))
TRANSFER_ANY = AnchorSpec(kinds=("transfer_approved", "transfer_pending"))
DEPOSIT = AnchorSpec(kinds=("deposit",), products=ACCOUNTS)
PAY_CC = AnchorSpec(kinds=("payment_approved",), products=frozenset({"Credit Card"}))
PAY_CC_CONTEXT = AnchorSpec(kinds=("payment_approved",), products=frozenset({"Credit Card"}), refs=False)  # disputes interest
PAY_CREDIT = AnchorSpec(kinds=("payment_approved",), products=CREDIT)
ADJ = AnchorSpec(kinds=("adjustment_approved",))
ADJ_CH = AnchorSpec(kinds=("adjustment_approved",), channels=frozenset({"App", "Web", "Branch", "ATM"}))
DEBIT_CH = AnchorSpec(kinds=DEBIT_KINDS, channels=NARRATABLE_CHANNELS)
DEBIT_ANY = AnchorSpec(kinds=DEBIT_KINDS + ("adjustment_approved",))
PENDING_CH = AnchorSpec(kinds=("purchase_pending", "transfer_pending", "other_pending"), channels=NARRATABLE_CHANNELS,
                        types=frozenset({"Purchase", "Withdrawal", "Transfer", "Payment", "Deposit"}))
ANY_DATE = AnchorSpec(kinds=DEBIT_KINDS + ("deposit",), refs=False)
BAL_PRODUCT = AnchorSpec(kinds=("purchase_approved", "deposit", "transfer_approved"), products=CARDS | ACCOUNTS, refs=False)
CREDIT_PRODUCT = AnchorSpec(kinds=("payment_approved",), products=CREDIT, refs=False)
STMT_PRODUCT = AnchorSpec(kinds=("purchase_approved", "deposit", "transfer_approved"),
                          products=frozenset({"Credit Card"}) | ACCOUNTS, refs=False)
ACCOUNT_PRODUCT = AnchorSpec(kinds=("deposit", "transfer_approved", "withdrawal_approved"), products=ACCOUNTS, refs=False)
CARD_PRODUCT = AnchorSpec(kinds=("purchase_approved",), products=CARDS, refs=False)
CARD_FREE_DATE = AnchorSpec(kinds=("purchase_approved",), products=CARDS, refs=False, free_date=True)
FREE_DATE = AnchorSpec(free_date=True, refs=False)
SYNTH_REFUND = AnchorSpec(synth="refund", refs=False)
SYNTH_FINANCING = AnchorSpec(synth="financing", refs=False)

# Families without slots: customer context only (no transaction_id), or no anchor at all.
CONTEXT_BY_INTENT = {
    "dispute_unrecognized_charge":
        AnchorSpec(kinds=("purchase_approved", "withdrawal_approved", "transfer_approved"), refs=False),
    "dispute_incorrect_charge_or_fee":
        AnchorSpec(kinds=("purchase_approved", "adjustment_approved", "withdrawal_approved"), refs=False),
    "account_payment_inquiry":
        AnchorSpec(kinds=("purchase_approved", "deposit", "transfer_approved", "purchase_declined"), refs=False),
    "card_lost_or_block": CARD_PRODUCT,
}
NO_ANCHOR = AnchorSpec(refs=False)

FAMILY_SPECS = {
    # Spanish
    "es-unrec-01": P, "es-unrec-03": P, "es-unrec-04": P_SUBS, "es-unrec-05": P, "es-unrec-06": P, "es-unrec-07": P_NOW,
    "es-unrec-08": P_SMALL, "es-unrec-09": ATM_W, "es-unrec-10": DEBIT_CH, "es-unrec-11": P, "es-unrec-12": P,
    "es-unrec-13": AnchorSpec(kinds=("transfer_approved",), products=ACCOUNTS), "es-unrec-14": P,
    "es-incorr-01": P, "es-incorr-02": P, "es-incorr-03": CARD_PRODUCT, "es-incorr-04": ADJ, "es-incorr-05": ATM_W,
    "es-incorr-06": P_STORE, "es-incorr-07": P_SUBS, "es-incorr-08": PAY_CC_CONTEXT, "es-incorr-09": P_INTL,
    "es-incorr-10": ADJ, "es-incorr-12": P_CC_INSTALMENTS, "es-incorr-13": ADJ, "es-incorr-14": P_DELIVERY,
    "es-inquiry-01": BAL_PRODUCT, "es-inquiry-03": PAY_CREDIT, "es-inquiry-04": P_DECL, "es-inquiry-05": TRANSFER_ANY,
    "es-inquiry-06": DEPOSIT, "es-inquiry-07": CREDIT_PRODUCT, "es-inquiry-10": P_STATUS, "es-inquiry-11": P_REV,
    "es-inquiry-12": STMT_PRODUCT,
    "es-card-03": CARD_PRODUCT, "es-card-04": P_SKIMMED, "es-card-05": CARD_FREE_DATE, "es-card-06": CARD_PRODUCT,
    "es-complaint-02": FREE_DATE,
    "es-ambig-03": P, "es-ambig-05": ADJ, "es-ambig-06": P_DECL, "es-ambig-08": ACCOUNT_PRODUCT, "es-ambig-09": P,
    "es-adv-02": SYNTH_REFUND, "es-adv-03": P, "es-adv-05": AnchorSpec(kinds=("transfer_approved",), products=ACCOUNTS),
    # Portuguese
    "pt-unrec-01": P, "pt-unrec-02": P, "pt-unrec-04": ACCOUNT_DEBIT, "pt-unrec-05": P, "pt-unrec-06": P,
    "pt-unrec-07": DEBIT_CH, "pt-unrec-08": P, "pt-unrec-09": P_SMALL_NOW, "pt-unrec-10": P_INTL, "pt-unrec-12": P,
    "pt-unrec-13": P_NOW, "pt-unrec-14": P,
    "pt-incorr-01": P, "pt-incorr-02": P, "pt-incorr-03": ADJ, "pt-incorr-04": ADJ, "pt-incorr-05": P_SUBS,
    "pt-incorr-06": P_STORE, "pt-incorr-07": ADJ, "pt-incorr-08": ATM_W, "pt-incorr-09": P_CC_INSTALMENTS,
    "pt-incorr-10": P_DOMESTIC,
    "pt-incorr-12": ADJ, "pt-incorr-13": ADJ_CH, "pt-incorr-14": P,
    "pt-inquiry-02": BAL_PRODUCT, "pt-inquiry-03": PAY_CC, "pt-inquiry-04": P_DECL, "pt-inquiry-05": TRANSFER_ANY,
    "pt-inquiry-07": P_STATUS, "pt-inquiry-09": DEPOSIT, "pt-inquiry-10": PENDING_CH, "pt-inquiry-11": ANY_DATE,
    "pt-card-02": CARD_FREE_DATE,
    "pt-oos-06": SYNTH_FINANCING,
    "pt-ambig-02": DEBIT_ANY, "pt-ambig-03": P, "pt-ambig-04": P, "pt-ambig-05": ADJ, "pt-ambig-06": P_DECL,
    "pt-ambig-08": P, "pt-ambig-10": DEBIT_ANY,
    "pt-adv-02": P, "pt-adv-05": TRANSFER, "pt-adv-08": SYNTH_REFUND,
}

IN_SCOPE_CONTEXT = ("dispute_unrecognized_charge", "dispute_incorrect_charge_or_fee", "account_payment_inquiry",
                    "card_lost_or_block")


def spec_for(family):
    """AnchorSpec for a family dict (families/<lang>.json)."""
    fid = family["family_id"]
    if fid in FAMILY_SPECS:
        return FAMILY_SPECS[fid]
    if family["requires"]:
        raise KeyError(f"{fid} has slots {family['requires']} but no anchor spec in anchor_specs.FAMILY_SPECS")
    if family["attack_type"] and family["intent"] == "out_of_scope":
        return NO_ANCHOR
    for intent in [family["intent"]] + list(family["acceptable_intents"]):
        if intent in CONTEXT_BY_INTENT:
            return CONTEXT_BY_INTENT[intent]
    return NO_ANCHOR
