# Source Index

The public repository contains only fictional source examples. Register a user's original XML,
JSON, YAML, CSV, or text definition through the explicit source-intake workflow; preserve the
bytes and record a hash. Source structure is evidence for event names, fields, and types. It does
not establish ownership, business meaning, or a confirmed metric rule.

```powershell
python scripts/xml_catalog.py <source-file> `
  --out <project-root>/sources/xml_catalog.json `
  --user-request "Register the supplied source definition" `
  --function-selection SOURCE_INTAKE
```

Planning tables and mutable mappings belong in the separate KNOWLEDGE workflow. A selected local
file remains unconfirmed until a user explicitly registers and binds it.
