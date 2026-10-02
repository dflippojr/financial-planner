"""Synthetic Apple Card export shape (issue #14). Invented values only."""

APPLE_CARD_HEADER = (
    "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD),Purchased By"
)

NATIVE_ROWS = (
    "09/15/2026,09/17/2026,SYNTHETIC COFFEE SHOP PURCHASE,Synthetic Coffee,Food & Drink,Purchase,4.50,Dana Example",
    "09/16/2026,09/16/2026,SYNTHETIC CARD PAYMENT THANK YOU,Synthetic Card Payment,Payments,Payment,-500.00,Dana Example",
    "09/17/2026,09/18/2026,SYNTHETIC MERCHANT REFUND,Synthetic Refund,Shopping,Credit,-15.00,Dana Example",
    "09/18/2026,09/18/2026,SYNTHETIC INTEREST CHARGE,Synthetic Interest,Fees & Adjustments,Debit,2.75,Dana Example",
    "09/19/2026,09/20/2026,SYNTHETIC GROCERY STORE PURCHASE,Synthetic Grocery,Groceries,Purchase,62.10,Dana Example",
    "09/19/2026,09/20/2026,SYNTHETIC GROCERY STORE PURCHASE,Synthetic Grocery,Groceries,Purchase,62.10,Dana Example",
    '09/20/2026,09/21/2026,SYNTHETIC STORE LONG DESCRIPTION,"Synthetic, Store",Shopping,Purchase,30.00,Dana Example',
    "09/21/2026,09/21/2026,SYNTHETIC BROKEN ROW,Synthetic Broken,Shopping",
)

OVERLAP_ROWS = (
    "09/18/2026,09/18/2026,SYNTHETIC INTEREST CHARGE,Synthetic Interest,Fees & Adjustments,Debit,2.75,Dana Example",
    "09/22/2026,09/23/2026,SYNTHETIC BOOKSTORE PURCHASE,Synthetic Bookstore,Shopping,Purchase,18.25,Dana Example",
)


def apple_card_csv(rows):
    return ("\r\n".join((APPLE_CARD_HEADER, *rows)) + "\r\n").encode("utf-8")


NATIVE_CSV = apple_card_csv(NATIVE_ROWS)
OVERLAP_CSV = apple_card_csv(OVERLAP_ROWS)
