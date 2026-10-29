---
name: invoice-list
description: List invoices or receipts from files in a folder into an Excel sheet (sorting, filtering, totals).
version: 1
tools: [list_dir, extract_invoice, write_xlsx]
---
# Instructions

1. Use `list_dir` on the folder the user pointed at (and its subfolders only
   if they asked for that) to find `.pdf`, `.jpg`, `.jpeg` and `.png` files.
2. Call `extract_invoice` **once per file**. Never call `read_pdf` or any
   other read tool on an invoice file — `extract_invoice` already did the
   reading, OCR and validation, and dumping a whole document into the chat
   defeats the point of this skill when a folder has dozens of files. Keep
   only the JSON that `extract_invoice` returns.
3. Never re-calculate or "correct" an amount, date, or any other field
   yourself. Use the values, `status` and `notes` exactly as
   `extract_invoice` returned them.
4. Build one row per file with these columns:
   - Source file
   - Vendor
   - Vendor NPWP
   - Invoice no.
   - Invoice date
   - Due date
   - Currency
   - Subtotal
   - Tax
   - Total
   - Status (`OK` / `Needs review`)
   - Notes (join `extract_invoice`'s `notes` with "; "; blank if none)
5. Sort as the user asked (default: by due date, ascending). A row with a
   missing sort key (e.g. no due date) goes **at the bottom**, never guessed
   at or dropped.
6. Write the rows with `write_xlsx` into the workspace. Default filename
   `invoices_<YYYY-MM-DD>.xlsx` (today's date); never overwrite an existing
   file without asking first. Put the header row in row 1, data starting at
   row 2. Write date columns as `YYYY-MM-DD` strings (the tool turns those
   into real Excel dates automatically) and amounts as plain numbers, not
   formatted strings.
7. If a file couldn't be read at all (`extract_invoice` returned an error),
   still add a row for it: Source file filled in, every other column blank,
   Status `Needs review`, and the error message in Notes.
8. Reply with a short summary: how many files were processed, how many need
   review and why (grouped by reason, not file-by-file), and any files that
   couldn't be read at all.

## Variations

- **Filter by vendor or month**: only include matching rows; mention the
  filter in the summary.
- **Totals per vendor/month, or per currency**: add a blank separator row
  then one totals row per group, summing the Subtotal/Tax/Total columns for
  that group's `OK` rows only — a row still under review shouldn't be folded
  into a total silently. Label it clearly (e.g. "TOTAL — Acme Corp").
- **Receipts instead of invoices**: same process; `extract_invoice` handles
  both (`doc_type` tells you which it found).
- **CSV instead of xlsx**: use `write_text` to write a comma-separated file
  with the same columns instead of `write_xlsx`.
