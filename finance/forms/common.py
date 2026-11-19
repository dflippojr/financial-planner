"""Limits, error messages and the account list every form shares."""







MIN_SIGNED_BIGINT = -(2**63)



MAX_SIGNED_BIGINT = 2**63 - 1



ALL_VISIBLE_ACCOUNTS = "All visible accounts"



END_DATE_ORDER_ERROR = "End date must be on or after start date."



AMOUNT_RANGE_ERROR = "Amount is outside the supported range."



# A recorded balance or statement contribution beyond $1 trillion is a typo.
MAX_BALANCE_MINOR = 100_000_000_000_000



BALANCE_RANGE_ERROR = "Enter an amount between -$1,000,000,000,000 and $1,000,000,000,000."
