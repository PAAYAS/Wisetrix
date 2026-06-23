"""Database (seed-data) artifact handling.

This package handles the DB side of a GTM upgrade. It is DOWNSTREAM of the app
upgrade: the app pipeline decides Merge/Retain/Remove per artifact, and this
package acts on that decision for `bizpolicydefs` artifacts whose data also
lives in the customer's *_db seed-data git repo as `bppol.*.xlsx` workbooks.

Nothing here touches the app pipeline — it only reads the app's outputs
(comparison results + merged bizpolicydef.json) and the DB git repo.

Modules:
    paths       — locate the bppol xlsx for an artifact in the DB repo
    xlsx_merge  — deterministic reconcile of a merged bizpolicydef.json into a
                  new bppol xlsx (openpyxl; no Claude)
    db_scan     — derive per-artifact DB actions from the app comparison
"""
