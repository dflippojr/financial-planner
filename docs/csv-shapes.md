# Describing a provider's CSV export without sharing it

Issue #1 needs the real column layout of each provider's export (Huntington Bank, Capital One, Apple Card, Vanguard): headers, date format, amount and sign conventions, and any extra rows. Real exports contain account numbers and transactions, so they must never be committed, attached to an issue, or pasted into a chat.

`scripts/csv_shape.py` reads an export on your own machine and prints only its shape.

```console
python scripts/csv_shape.py path/to/export.csv --label "Huntington checking"
```

By default nothing from the file is printed as written, including column headers. Open the file first and look at its first lines. If they are only column names (no account holder, no account number, no date range line), run it again with `--show-headers` so the output names each column:

```console
python scripts/csv_shape.py path/to/export.csv --label "Huntington checking" --show-headers
```

It needs Python 3 and nothing else. Run it once per provider and per account type that exports differently (for example checking and credit card), then share the output.

## What it prints

- File facts: encoding (UTF-8, Windows-1252, or UTF-16 detected from its byte-order mark), line endings, delimiter, row count, and how many rows have each column count (a second column count usually means a summary or a second section, as some brokerage exports have).
- The header row: the row just above the first row that contains a date. Its labels are masked by default (`col 3 (masked: Aaaa Aaaa)`), because nothing in a file can tell a header from a line that names a person or account, even something as ordinary-looking as `Price,Account,USD`. With `--show-headers`, labels made of letters and simple punctuation are printed as written; a label containing digits is still masked.
- Masked examples of any rows that do not fit the main layout, such as a summary line or a second section, with how many columns they have.
- For each column: its kind (date, money, or text), how many rows are empty, and its masked patterns. Masking turns every letter into `a` or `A` and every digit into `9`, so `09/27/2026` becomes `99/99/9999` and a 16-digit card number becomes `9{16}`.
- For date columns, whether day or month comes first, judged only from whether a field ever exceeds 12.
- For money columns, how many values are negative, whether thousands separators, a decimal point or comma, currency symbols, or parentheses are used, and whether a pair of columns looks like separate debit and credit columns.
- Any lines before the header, masked.

## Seeing a fixed vocabulary, such as transaction types

Masking hides everything, which is right for descriptions but hides what we most need to learn from a brokerage export: the list of transaction types (buy, dividend, contribution, and so on). For a column that holds a fixed vocabulary, ask for its distinct values explicitly (name the column as it appears in the file, or as `col N`):

```console
python scripts/csv_shape.py path/to/export.csv --show-headers --show-values "Transaction Type"
```

The values are shown only when the column has at most 25 distinct values and every value is plain letters, spaces, and simple punctuation. A column with digits or many distinct values is refused. You choose the column, so use this only for categories, never for descriptions, payees, or anything that names a person, merchant, or account.

## What it never prints

Unless you use `--show-headers` or `--show-values`, it never prints any text from the file: no cell value from a data row, no preamble line, and no header label. Before printing, it checks its own report against every cell in the file and prints nothing if any value would appear.

## Before you share the output

Read it once. The tool is conservative but you know your data, and the two options that print text are your choice because only you can look at the file. If anything in the report looks like real data, do not share it and tell us.

Never commit an export, its output for a real account with your own edits, or anything derived from a statement. The tool's tests use only synthetic data.
