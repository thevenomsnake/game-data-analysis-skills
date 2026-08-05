# Contributing

Contributions should preserve the public edition's narrow contract: file-backed SQL,
immutable versions, searchable metadata, and exact delivery receipts.

Before opening a pull request:

1. Keep organization-specific schemas, table names, rules, URLs, and credentials out of the
   repository.
2. Add or update standard-library tests for behavior changes.
3. Run:

   ```powershell
   python -m unittest discover -s .\sql-engineering\tests -p "test_*.py"
   python -m py_compile .\sql-engineering\scripts\sql_workspace.py
   ```

4. Explain the user-visible behavior and migration impact in the pull request.

New dependencies require a concrete reason and should not be added for behavior available in
the Python standard library.
